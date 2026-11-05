# Как работает код

Поведение системы, алгоритм и сценарии запуска описаны в [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md). Здесь — почему реализация работает именно так. Фрагменты относятся к указанным функциям; сокращения отмечены явно, импорты обычно опущены. Детали HTTP-клиента сверены с установленным `aiohttp 3.14.5`.

## 1. Вход в программу и конфигурация

Команда `python -m highloads_applications.balancer` исполняет [balancer/__main__.py](src/highloads_applications/balancer/__main__.py). `argparse` разбирает CLI, `load_config` создаёт конфигурацию, `web.run_app` управляет запуском HTTP-сервера и остановкой. У бэкенда аналогичный вход; генератор запускает свою верхнюю корутину через `asyncio.run`.

В [configuration.py](src/highloads_applications/configuration.py) поток данных такой:

```text
TOML → словарь → dataclass(**поля) → __post_init__ → SystemConfig
```

`tomllib` читает TOML из файла, открытого в бинарном режиме. `**data["balancer"]` разворачивает словарь в именованные аргументы конструктора: лишний или отсутствующий аргумент вызывает `TypeError`. Затем `__post_init__` проверяет значения — аннотации типов сами этого не делают.

`@dataclass(frozen=True)` генерирует конструктор и запрещает обычное переназначение полей. Чтобы поменять настройки, создают новый объект:

```python
from dataclasses import replace
from highloads_applications.configuration import load_config

original = load_config().loadgen
parallel = replace(original, mode="concurrent", concurrency=5)
assert original.mode == "sequential"  # При штатном system.toml.
assert parallel.concurrency == 5
```

`replace` снова вызывает конструктор и `__post_init__`: переопределения CLI проходят те же проверки. Например, `concurrency=0` будет отклонено. `SystemConfig` объединяет настройки; уникальность ID и адресов проверяется отдельно в `load_config`.

В валидаторах есть две неочевидные защиты: `bool` явно исключается из чисел, поскольку в Python `isinstance(True, int)` истинно; `math.isfinite` отвергает `NaN` и бесконечности, которые ломали бы сравнения и расчёты. `DEFAULT_CONFIG` вычисляется от `__file__`, поэтому поиск штатного конфига не зависит от текущего каталога.

## 2. Бэкенд: замыкание и неблокирующее ожидание

В [backend/app.py](src/highloads_applications/backend/app.py) `create_app` создаёт словарь `state` и вложенные обработчики `work`, `health`, `admin`. Они сохраняют доступ к одному словарю через **замыкание**. У каждого вызова `create_app` свой словарь. `BackendState(TypedDict)` описывает его форму для проверки типов; в памяти это обычный `dict`, без автоматической проверки значений.

Рабочая часть `work`, после проверки ID и доступности:

```python
delay = state["delay_ms"]
status = state["work_status"]

await asyncio.sleep(delay / 1000)

# Между ожиданием и ответом в исходнике записывается лог.
return web.json_response({"request_id": value, "backend_id": config.id}, status=status)
```

`async def` объявляет корутину; её обычный вызов ещё не запускает работу. `await` исполняет ожидаемую операцию и позволяет текущей задаче приостановиться, если результат ещё не готов. `create_task` планирует отдельную задачу в event loop. Это основы используемой здесь модели конкурентности. [Семантика корутин и задач в Python](https://docs.python.org/3.13/library/asyncio-task.html#coroutines).

Во время `sleep` обработчик не занимает поток вычислениями, и сервер обслуживает другие запросы, в том числе `/health`. Поэтому три одновременных запроса с задержкой 50 мс могут завершиться примерно за 50 мс плюс накладные расходы. `time.sleep` или долгий вычислительный цикл внутри обработчика заблокировали бы весь его event loop.

`delay` и `status` считываются **до ожидания**: изменение `state` администратором во время запроса не поменяет уже выбранные параметры. `admin` сначала проверяет весь JSON и только затем делает один `state.update`. Ошибка в одном поле не оставит частично применённое обновление.

## 3. Алгоритм отделён от HTTP

[balancer/algorithms.py](src/highloads_applications/balancer/algorithms.py) не знает о сокетах, маршрутах или `aiohttp`. `LeastTime` выбирает объект состояния, а `Balancer` выполняет сетевую работу. Поэтому выбор можно тестировать на числах без запуска серверов.

Здесь `BackendState` — уже **изменяемый dataclass**, отдельный от одноимённого `TypedDict` бэкенда. Он содержит конфиг сервера, доступность, оценку, число активных запросов и счётчики проверок.

Основная ветка `select_backend`:

```python
available = [backend for backend in self.backends if backend.available]
if not available:
    return None

self.assignments += 1
if self.assignments % self.exploration_interval != 0:
    return min(available, key=lambda backend: backend.estimate)
```

`key` задаёт величину сравнения, но `min` возвращает сам объект `BackendState`. При равенстве он берёт первый встреченный объект; конструктор заранее сортирует серверы по ID. Счётчик растёт после проверки наличия кандидатов: запрос без назначения не меняет период исследующего обхода.

Вторая ветка метода:

```python
while True:
    backend = self.backends[self.exploration_position]
    self.exploration_position = (self.exploration_position + 1) % len(self.backends)
    if backend.available:
        return backend
```

Остаток от деления замыкает индекс в кольцо. Указатель сдвигается и при пропуске недоступного сервера. Цикл завершится: выше уже найден хотя бы один доступный кандидат, а между проверкой и обходом нет `await`, позволяющего другой задаче изменить доступность. Выбор занимает O(B), где B — число бэкендов.

Начальное `estimate=float("inf")` означает отсутствие измерения; одновременно `available=False` запрещает выбор. Успешный `probe` **присваивает** измеренное время. Это существенно: при `alpha<1` сглаживание конечного значения с бесконечностью оставило бы бесконечность. Обычные ответы затем используют `record_response`:

```python
if successful and backend.available:
    backend.estimate = ewma(backend.estimate, duration, self.alpha)
```

Здесь проверяется текущее состояние, а не доступность в момент назначения. Поздний успешный ответ исключённого сервера не меняет оценку.

## 4. Учёт запроса: что сохраняется до await и зачем finally

Цепочка обработки в [balancer/app.py](src/highloads_applications/balancer/app.py):

```text
work(request) → parse_request_id(request) → handle_work(id)
             → select_backend() → forward() → record_response()
```

`parse_request_id` — импортированный под другим именем помощник из [http_helpers.py](src/highloads_applications/http_helpers.py). Он использует `request.query.getall`, чтобы отклонить повторяющиеся `request_id`, а `isascii` вместе с `isdecimal` разрешает только десятичные ASCII-цифры. `raise web.HTTPBadRequest(...)` превращается средствами `aiohttp` в HTTP 400.

Ниже сокращённый фрагмент `handle_work` после успешного выбора бэкенда; убраны измерение EWMA для логов, сами логи и заголовки:

```python
number = self.algorithm.assignments
estimate_at_assignment = backend.estimate
backend.active += 1
task = asyncio.current_task()
assert task is not None
self.work_tasks.add(task)
started = perf_counter()
status = None
try:
    response = await self.forward(backend, request_id)
    status = response.status
finally:
    duration = perf_counter() - started
    backend.active -= 1
    self.algorithm.record_response(backend, duration, successful=status == 200)
    self.work_tasks.discard(task)
```

`current_task()` возвращает уже существующую задачу обработчика, не создавая новую. Она записывается в `work_tasks`, чтобы при остановке можно было дождаться незавершённой работы. `perf_counter` — монотонный таймер для измерения интервалов, а не календарное время.

Номер назначения и исходная оценка копируются в локальные переменные: пока выполняется `forward`, другие запросы могут изменить общее состояние. Участки выбора и обновления не содержат `await`, поэтому в используемом одном event loop не перемежаются с другими задачами. Это свойство структуры кода, не общая потокобезопасность класса.

`finally` уменьшает `active` и удаляет задачу даже при исключении или отмене. Если `forward` не вернул ответ, `status` остаётся `None`, и измерение не принимается за успешное. `discard` удаляет элемент множества без ошибки, если его уже нет.

**Особенность конкурентных ответов.** Допустим, два запроса назначены A при оценке 50 мс. Второй завершился за 100 мс и обновил её до 60 мс. Первый затем завершился за 500 мс: при `alpha=0.2` он обновит **текущие 60 мс** до 148 мс. Его `X-EWMA-Before` всё ещё содержит 0.05 — снимок при назначении. Лог `ewma_before` при завершении покажет 0.06. Заголовок нельзя безоговорочно подставлять в формулу для восстановления `X-EWMA-After`.

## 5. aiohttp: клиентская сессия, тело ответа и повторы

Сетевая часть `forward`, до формирования ответа клиенту:

```python
async with self.session.get(
    backend.config.url + "/work",
    params={"request_id": request_id},
    timeout=self.work_timeout,
    allow_redirects=False,
) as response:
    body = await response.read()
    content_type = response.headers.get("Content-Type", "application/json")
    return web.Response(
        status=response.status,
        body=body,
        headers={"Content-Type": content_type},
    )
```

`ClientSession` управляет исходящими HTTP-запросами, `TCPConnector` — их соединениями. `async with` освобождает ресурсы ответа при выходе, а `response.read()` загружает всё тело. Получение заголовков ещё не означает успешное чтение ответа целиком. [API сессии и ответа](https://docs.aiohttp.org/en/stable/client_reference.html).

Для проекта это означает: обрыв после заголовков не станет успешным измерением; тело полностью буферизуется в памяти, после чего создаётся новый `web.Response`. HTTP-статус 500 сам по себе здесь не возбуждает исключение: `raise_for_status` не включён, статус передаётся как данные. `allow_redirects=False` не даёт превратить ответ 3xx в незаметный дополнительный запрос.

У балансировщика сессия одна на всё приложение, но коннектор создан с `force_close=True, limit=0`: повторное использование соединений отключено, собственного лимита соединений нет. У генератора `TCPConnector(limit=config.concurrency)` допускает keep-alive и ограничивает число одновременных соединений. Общая сессия поэтому не означает одинаковый режим соединений. [Параметры коннектора](https://docs.aiohttp.org/en/stable/client_reference.html#aiohttp.BaseConnector).

`ClientTimeout(total=..., ceil_threshold=math.inf)` задаёт общий срок обращения. `total` включает чтение тела; бесконечный порог отключает округление общего дедлайна вверх до целой секунды. Тайм-аут — не гарантия пробуждения с точностью до миллисекунды: event loop должен получить возможность обработать его.

Особая деталь находится в `single_attempt` из [http_helpers.py](src/highloads_applications/http_helpers.py):

```python
async def single_attempt(request: ClientRequest, handler: ClientHandlerType) -> ClientResponse:
    try:
        return await handler(request)
    except (ClientOSError, ServerDisconnectedError) as error:
        raise ClientConnectionError(str(error)) from error
```

Это **клиентский middleware**: обёртка вокруг отправки запроса, зарегистрированная через `middlewares=(single_attempt,)`. В `aiohttp 3.14.5` внутренний клиент может повторить GET при двух указанных классах ошибок, даже с `force_close=True`. Обёртка преобразует их в общий `ClientConnectionError`, для которого эта ветка повтора не применяется. `from error` сохраняет исходную причину исключения. Без этой детали один вызов `get` не обязательно означал бы одну попытку отправки.

`forward` ловит `TimeoutError` и создаёт 504; остальные `ClientError` превращает в 502. После этого `handle_work` добавляет метаданные назначения и для ошибочного ответа.

## 6. Жизненный цикл приложения и отмена

В `create_app` данные приложения связываются с типизированными ключами:

```python
CONFIG = web.AppKey("config", SystemConfig)
RUNTIME = web.AppKey("balancer", Balancer)
```

`app[CONFIG]` хранит настройки, `app[RUNTIME]` — экземпляр работающего балансировщика. `AppKey` позволяет анализатору типов связать ключ с типом значения; это не конструктор и не преобразование данных.

`app.cleanup_ctx.append(lifecycle)` регистрирует асинхронный генератор с одним `yield`. Функция без аннотаций и логирования:

```python
async def lifecycle(app):
    async with ClientSession(
        connector=TCPConnector(force_close=True, limit=0),
        middlewares=(single_attempt,),
    ) as session:
        balancer = Balancer(app[CONFIG], session)
        app[RUNTIME] = balancer
        try:
            for backend in balancer.algorithm.backends:
                await balancer.probe(backend)
            for backend in balancer.algorithm.backends:
                task = asyncio.create_task(
                    balancer.check_health(backend),
                    name=f"health-{backend.config.id}",
                )
                balancer.health_tasks.append(task)
            yield
        finally:
            for task in balancer.health_tasks:
                task.cancel()
            await asyncio.gather(*balancer.health_tasks, return_exceptions=True)
```

Код до `yield` выполняется при старте, продолжение — при очистке. Поэтому сессия существует всё время работы приложения, а обслуживать клиентов балансировщик начинает после начальных проб. `try/finally` охватывает и подготовку, чтобы частично выполненный запуск тоже мог освободить ресурсы. [Механизм cleanup context](https://docs.aiohttp.org/en/stable/web_advanced.html#cleanup-context).

При остановке отдельный обработчик `on_shutdown` вызывает `drain`: выставляет `stopping`, ждёт рабочие задачи через `asyncio.wait` с тайм-аутом, отменяет оставшиеся и дожидается их завершения. Затем `lifecycle` останавливает проверки, и выход из контекста закрывает HTTP-сессию.

`task.cancel()` лишь запрашивает отмену. `gather(..., return_exceptions=True)` нужен, чтобы дождаться завершения задач и их очистки, собрав исключения как результаты. Один вызов `cancel()` ещё не означает, что соединения уже закрыты.

Оба HTTP-сервера запускаются с `handler_cancellation=True`: потеря клиентского соединения отменяет задачу обработчика. В балансировщике отмена проходит через `forward`, закрывает контекст исходящего запроса и приводит в `finally` учёта. [Отмена обработчиков в aiohttp](https://docs.aiohttp.org/en/stable/web_advanced.html#web-handler-cancellation).

`asyncio.CancelledError` наследует `BaseException`, а не обычный `Exception`; обработчики сетевых ошибок его не перехватывают. В CLI генератора отмена обрабатывается на верхнем уровне: отчёт записывается только после завершённого `generate`, поэтому прерванный запуск не сохраняет частичные результаты.

## 7. Проверки здоровья — отдельная задача на каждый бэкенд

`lifecycle` запускает `check_health(backend)` через `asyncio.create_task` для каждого сервера. Их жизненный цикл независим от клиентских обработчиков; медленная проверка одного сервера не заставляет ждать остальные.

Начало цикла в `check_health`:

```python
while True:
    await asyncio.sleep(self.settings.health_interval)
    if not backend.available and backend.active > 0:
        continue

    success = await self.is_healthy(backend)
    # Дальше — обновление счётчиков отказа или восстановления.
```

Дальнейшие переходы описаны в обзоре проекта. Существенная деталь реализации — восстановительная `probe` вызывается через `await` в **той же задаче**, после достаточной серии успешных проверок. Проверки и проба одного сервера не перекрываются.

Интервал — пауза в начале итерации, а не расписание «строго раз в секунду»: к нему добавляется длительность проверки и возможной пробы. Условие `active > 0` не позволяет вернуть сервер в работу раньше завершения его старых запросов; иначе их запоздалые ответы могли бы изменить уже обновлённую оценку.

## 8. Генератор: три разных механизма конкурентности

В [loadgen/generator.py](src/highloads_applications/loadgen/generator.py) последовательный режим просто делает `await send_request(...)` внутри цикла. В конкурентном используются семафор, группа задач и ограничение соединений. Ядро этой ветки:

```python
semaphore = asyncio.Semaphore(config.concurrency)

async def send_limited(request_id: int) -> RequestResult:
    async with semaphore:
        return await send_request(session, url, request_id)

tasks = []
async with asyncio.TaskGroup() as group:
    for request_id in range(1, config.requests + 1):
        tasks.append(group.create_task(send_limited(request_id)))
results = [task.result() for task in tasks]
```

| Механизм | Зачем он здесь |
| --- | --- |
| `Semaphore(C)` | Допускает не более C задач внутрь `send_request`; выход из `async with` освобождает разрешение. |
| `TaskGroup` | Владеет задачами и ждёт их завершения при выходе из блока. При необработанной ошибке одной задачи отменяет остальные. |
| `TCPConnector(limit=C)` | Ограничивает одновременные соединения на уровне HTTP-клиента. |

`TaskGroup` группирует необработанные ошибки в `ExceptionGroup`; обычные сетевые сбои здесь уже превращены в `RequestResult.error` внутри `send_request`, поэтому сами по себе серию не прерывают. [Правила TaskGroup](https://docs.python.org/3.13/library/asyncio-task.html#task-groups).

Таймер запроса запускается **внутри** `send_request`, после семафора. Общий таймер серии запускается раньше создания сессии. Так код разделяет ожидание своей очереди и длительность HTTP-операции.

`tasks` добавляются по порядку ID, а `task.result()` вызывается после выхода из группы, когда задачи завершены. Поэтому результаты идут по ID независимо от порядка завершения; сортировка не нужна. Все N задач создаются сразу: семафор ограничивает выполняющуюся работу, но не расход памяти на задачи. Здесь он O(N), как и размер итогового отчёта.

## 9. Статистика

`RequestResult` — dataclass одной попытки. `asdict` превращает его в данные для JSON; `Counter` считает распределения по бэкендам и ошибкам. Успех требует одновременно `status == 200` и `error is None`: заголовки с 200 могли прийти до ошибки чтения тела. JSON бэкенда генератор не проверяет, данные о назначении берёт из заголовков.

`percentile` использует линейную интерполяцию между соседними элементами **уже отсортированного** списка:

```python
from highloads_applications.loadgen.generator import percentile

values = [0.01, 0.02, 0.04, 0.08]
assert percentile(values, 0.5) == 0.03
# Позиция: (4 − 1) × 0.5 = 1.5; середина между values[1] и values[2].
```

`latency_statistics` сама сортирует длительности. p50 вычисляется от двух результатов, p95 — от двадцати, p99 — от ста; иначе поле равно `None`, в JSON — `null`. Это пороги данной реализации, а не гарантия статистической надёжности оценки хвоста.

[compare_ewma.py](src/highloads_applications/loadgen/compare_ewma.py) вызывает ту же функцию `ewma`, что и балансировщик: 10 быстрых, 20 медленных, 30 быстрых измерений для каждого из двух `alpha`, всего 120 строк. `csv.DictWriter` записывает словари по именам колонок. `matplotlib` импортируется только внутри `plot`, поэтому группа зависимостей `plots` нужна лишь при `--plot`.
