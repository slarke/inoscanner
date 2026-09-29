"""Описание API в формате OpenAPI 3.0 (для Swagger UI на /docs).

aiohttp не генерирует спецификацию сам, поэтому она описана здесь вручную,
но адреса регистров и ограничения берутся из :mod:`plc_api.ladder`, чтобы
описание не расходилось с тем, что реально делает сервер. Соответствие путей
спецификации и маршрутов приложения проверяет tests/test_api_docs.py.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from plc_panel.config import AXES, AXIS_SAFETY, IO

from . import __version__
from .ladder import AXIS_NAMES, PULSE_AXES

API = "/api/v1"


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict, description: str = "", example=None) -> dict:
    media = {"schema": schema}
    if example is not None:
        media["example"] = example
    return {"description": description, "content": {"application/json": media}}


_ERROR_TEXT = {
    "400": "Некорректный запрос: не JSON, неизвестное поле, неверный тип или значение",
    "401": "Нет или неверный Bearer-токен",
    "404": "Неизвестная ось",
    "409": "Ось заблокирована концевиком или не подключена (active_axes); "
           "для wait — ожидание прервано стопом или новой командой оси (aborted)",
    "422": "Уставка вне мягких пределов или функция не реализована текущим ладдером",
    "502": "ПЛК отклонил запрос или обмен Modbus сорвался",
    "503": "Нет связи с ПЛК",
    "504": "Команда не выполнена за 5 с (или ось не пришла за timeout_s)",
}


def _errors(*codes: str) -> Dict[str, dict]:
    return {c: _json(_ref("Error"), _ERROR_TEXT[c]) for c in codes}


def _body(properties: dict, required: List[str], example: dict) -> dict:
    return {
        "required": True,
        "content": {"application/json": {
            "schema": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
            "example": example,
        }},
    }


def _num(description: str, **extra) -> dict:
    return {"type": "number", "description": description, **extra}


def _bool(description: str, default: Optional[bool] = None) -> dict:
    s = {"type": "boolean", "description": description}
    if default is not None:
        s["default"] = default
    return s


def _regs(pick) -> str:
    """'X: …, Y: …, Z: …' по функции оси -> строка адресов."""
    return "; ".join(f"{AXIS_NAMES[n]}: {pick(n)}" for n in AXES)


def _command(summary: str, description: str, body: Optional[dict] = None,
             errors=("400", "401", "404", "502", "503", "504"),
             tag: str = "Оси", responses: Optional[dict] = None) -> dict:
    op = {
        "tags": [tag],
        "summary": summary,
        "description": description,
        "responses": {
            **(responses or {"202": _json(_ref("CommandSent"),
                                          "Регистры и строб записаны в ПЛК")}),
            **_errors(*errors),
        },
    }
    if body is not None:
        op["requestBody"] = body
    return {"post": op}


def build_spec(token_required: bool) -> dict:
    axis_param = {
        "name": "axis", "in": "path", "required": True,
        "description": "Ось: x|y|z или 1|2|3",
        "schema": {"type": "string",
                   "enum": [a.lower() for a in AXIS_NAMES.values()]
                   + [str(n) for n in AXES]},
    }
    pulse_axes = ", ".join(AXIS_NAMES[n] for n in sorted(PULSE_AXES))
    motion_errors = ("400", "401", "404", "409", "422", "502", "503", "504")
    r = AXES

    paths: Dict[str, dict] = {
        f"{API}/health": {"get": {
            "tags": ["Состояние"], "summary": "Состояние связи с ПЛК",
            "responses": {"200": _json(_ref("Health"), "OK"), **_errors("401")},
        }},
        f"{API}/capabilities": {"get": {
            "tags": ["Состояние"],
            "summary": "Что поддерживает текущий MAIN.LD",
            "description": "Адреса регистров по осям, ограничения ладдера, "
                           "active_axes, мягкие пределы, допуск приезда.",
            "responses": {"200": _json({"type": "object"}, "OK"), **_errors("401")},
        }},
        f"{API}/telemetry": {"get": {
            "tags": ["Состояние"], "summary": "Последний кадр опроса ПЛК",
            "responses": {"200": _json(_ref("Telemetry"), "OK"),
                          **_errors("401", "503")},
        }},
        API + "/axes/{axis}": {
            "parameters": [axis_param],
            "get": {
                "tags": ["Состояние"], "summary": "Телеметрия одной оси",
                "responses": {"200": _json(_ref("AxisState"), "OK"),
                              **_errors("401", "404", "503")},
            },
        },
        API + "/axes/{axis}/power": _command(
            "MC_Power: включить/выключить привод",
            "Уровень на катушке MC_Power (" + _regs(lambda n: f"M{r[n].power}") + ").",
            _body({"on": _bool("true — Servo ON")}, ["on"], {"on": True})),
        API + "/axes/{axis}/reset": _command(
            "MC_Reset: сброс ошибок оси",
            "Строб 100 мс (" + _regs(lambda n: f"M{r[n].reset}") + ")."),
        API + "/axes/{axis}/move-absolute": _command(
            "MC_MoveAbsolute: движение в абсолютную точку",
            "Пишет позицию и скорость (REAL) и даёт строб Execute ("
            + _regs(lambda n: f"D{r[n].abs_pos}/D{r[n].abs_spd}, M{r[n].abs_exec}")
            + ").\n\nБез `wait` ответ 202 означает «команда записана», а не «ось "
              "приехала». С `wait: true` сервер ждёт кадр телеметрии с осью в "
              "точке (допуск `arrival_tolerance_mm`) и отвечает 200.\n\n"
              f"`pulse` (импульс замера по приезду, M14) реализован в ладдере "
              f"только для оси {pulse_axes}; для остальных — 422.",
            _body({
                "position": _num("Целевая позиция, мм"),
                "speed": _num("Скорость, > 0", exclusiveMinimum=True, minimum=0),
                "pulse": _bool("Импульс замера по приезду (только X)", False),
                "wait": _bool("Ждать приезда по телеметрии", False),
                "timeout_s": _num("Сколько ждать приезда, с", default=60),
            }, ["position", "speed"],
                {"position": 100.0, "speed": 10.0, "pulse": False, "wait": True,
                 "timeout_s": 30}),
            errors=motion_errors,
            responses={
                "200": _json(_ref("Arrived"), "С wait: ось в точке"),
                "202": _json(_ref("CommandSent"), "Без wait: команда записана"),
            }),
        API + "/axes/{axis}/move-relative": _command(
            "MC_MoveRelative: смещение на величину",
            "Пишет смещение и скорость и даёт строб ("
            + _regs(lambda n: f"D{r[n].rel_distance}/D{r[n].rel_speed}, "
                              f"M{r[n].rel_exec}")
            + "). Пределы проверяются от текущей позиции.",
            _body({"distance": _num("Смещение, мм (знак — направление)"),
                   "speed": _num("Скорость, > 0")},
                  ["distance", "speed"], {"distance": 5.0, "speed": 10.0}),
            errors=motion_errors),
        API + "/axes/{axis}/move-velocity": _command(
            "MC_MoveVelocity: движение со скоростью",
            "Пишет 3×REAL (скорость, разгон, торможение) и даёт строб ("
            + _regs(lambda n: f"D{r[n].vel_block}…D{r[n].vel_block + 5}, "
                              f"M{r[n].vel_exec}")
            + "). Пределы не проверяются — ось едет до stop или концевика.",
            _body({"speed": _num("Скорость, ≠ 0"),
                   "acceleration": _num("Разгон, > 0"),
                   "deceleration": _num("Торможение, > 0")},
                  ["speed", "acceleration", "deceleration"],
                  {"speed": 5.0, "acceleration": 20.0, "deceleration": 20.0}),
            errors=motion_errors),
        API + "/axes/{axis}/stop": _command(
            "MC_Stop: остановить ось",
            "Строб MC_Stop (" + _regs(lambda n: f"M{r[n].stop_exec}") + "), "
            "с приоритетом над ожидающими командами. Замедление задаётся "
            "переменными ПЛК (mc_stop_decceleration_*), из API не меняется."),
        API + "/axes/{axis}/set-position": _command(
            "MC_SetPosition: переопределить текущую координату",
            "Пишет новое значение и даёт строб ("
            + _regs(lambda n: f"D{r[n].set_pos_val}, M{r[n].set_pos_exec}") + ").",
            _body({"position": _num("Новая координата текущей точки, мм")},
                  ["position"], {"position": 0.0})),
        API + "/axes/{axis}/unblock": _command(
            "Снять блокировку по концевику",
            "Строб 100 мс на катушке сброса защёлки ("
            + _regs(lambda n: f"M{AXIS_SAFETY[n].block_reset} → сброс "
                              f"M{AXIS_SAFETY[n].block_latch}") + ")."),
        f"{API}/stop-all": _command(
            "Стоп всех осей",
            "MC_Stop на всех осях одновременно, с приоритетом; при "
            "`power_off: true` затем снимается MC_Power. Программный стоп через "
            "Modbus — аппаратный аварийный стоп он не заменяет.",
            _body({"power_off": _bool("Снять питание после стопа", False)},
                  [], {"power_off": False}),
            errors=("400", "401", "502", "503", "504"), tag="Общие"),
        f"{API}/io/y0/pulse": _command(
            "Импульс на выходе Y0",
            f"Строб M{IO.Y0_SET} (SET Y0) → 150 мс → строб M{IO.Y0_RESET} (RST Y0).",
            errors=("401", "502", "503", "504"), tag="Общие"),
        f"{API}/io/y0": {"put": {
            "tags": ["Общие"],
            "summary": "Статический уровень Y0",
            "description": f"HIGH: удерживать M{IO.Y0_SET}. LOW: отпустить "
                           f"M{IO.Y0_SET} и дать строб M{IO.Y0_RESET} (удерживаемый "
                           f"M{IO.Y0_RESET} глушил бы авто-импульсы замера).",
            "requestBody": _body({"high": _bool("true — HIGH")}, ["high"],
                                 {"high": True}),
            "responses": {"202": _json(_ref("CommandSent"), "Записано"),
                          **_errors("400", "401", "502", "503", "504")},
        }},
        f"{API}/ws": {"get": {
            "tags": ["WebSocket"],
            "summary": "Поток телеметрии и событий (WebSocket)",
            "description": (
                "Swagger UI не умеет WebSocket — используйте вкладку "
                "**WebSocket** на этой странице.\n\n"
                "Сервер → клиент: `hello`, `telemetry`, `connection`, `command`, "
                "`arrived`, `blocked`. Клиент → сервер: `{\"type\": \"ping\"}`, "
                "`{\"type\": \"subscribe\", \"events\": [...], "
                "\"telemetry_interval_ms\": 200}`. Команды через WebSocket не "
                "принимаются. Токен для браузера — параметр `token`."),
            "parameters": [
                {"name": "events", "in": "query", "required": False,
                 "description": "Фильтр событий через запятую",
                 "schema": {"type": "string",
                            "example": "telemetry,arrived,connection"}},
                {"name": "telemetry_interval_ms", "in": "query", "required": False,
                 "description": "Прореживание телеметрии, мс",
                 "schema": {"type": "integer", "minimum": 0}},
                {"name": "token", "in": "query", "required": False,
                 "description": "Bearer-токен (браузер не умеет ставить заголовки WS)",
                 "schema": {"type": "string"}},
            ],
            "responses": {"101": {"description": "Переключение на WebSocket"},
                          **_errors("400", "401")},
        }},
    }

    nullable_bool = {"type": "boolean", "nullable": True}
    nullable_num = {"type": "number", "nullable": True}
    schemas = {
        "Error": {
            "type": "object", "required": ["error"],
            "properties": {"error": {
                "type": "object", "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "enum": [
                        "bad_request", "unauthorized", "not_found", "conflict", "aborted",
                        "out_of_limits", "unsupported_by_ladder", "plc_io_error",
                        "plc_not_connected", "timeout"]},
                    "message": {"type": "string"},
                }}},
        },
        "CommandSent": {
            "type": "object", "required": ["command", "axis", "params", "status"],
            "properties": {
                "command": {"type": "string", "example": "move_absolute"},
                "axis": {"type": "string", "nullable": True, "example": "X"},
                "params": {"type": "object"},
                "status": {"type": "string", "enum": ["sent"]},
            },
        },
        "Arrived": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "axis": {"type": "string"},
                "params": {"type": "object"},
                "status": {"type": "string", "enum": ["arrived"]},
                "state": _ref("AxisState"),
            },
        },
        "AxisState": {
            "type": "object",
            "description": "null — значение не удалось прочитать",
            "properties": {
                "axis": {"type": "integer"},
                "active": {"type": "boolean", "description": "Ось в active_axes"},
                "position": {**nullable_num, "description": "Фактическая позиция, мм"},
                "velocity": {**nullable_num, "description": "Фактическая скорость"},
                "power_command": {**nullable_bool, "description":
                                  "Команда MC_Power (M10/M20/M30), а не статус привода"},
                "blocked": {**nullable_bool, "description": "Защёлка блокировки по концевику"},
                "limit_left": {**nullable_bool, "description":
                               "Концевик (discrete input; адрес не подтверждён)"},
                "limit_right": nullable_bool,
            },
        },
        "Telemetry": {
            "type": "object",
            "properties": {
                "timestamp": {"type": "number", "description": "Unix time кадра"},
                "axes": {"type": "object",
                         "properties": {a: _ref("AxisState") for a in AXIS_NAMES.values()}},
                "errors": {"type": "object", "properties": {
                    "block_error_id": {"type": "integer", "description": "D200"},
                    "servo_error_id": {"type": "integer",
                                       "description": "D220, общий для всех осей"},
                    "axis_error_id": {"type": "integer",
                                      "description": "D222, общий для всех осей"},
                    "shared_between_axes": {"type": "boolean"},
                }},
                "y0": {"type": "object", "properties": {
                    "set_command": nullable_bool, "reset_command": nullable_bool}},
            },
        },
        "Health": {
            "type": "object",
            "properties": {
                "version": {"type": "string"},
                "connected": {"type": "boolean"},
                "detail": {"type": "string"},
                "telemetry_age_s": {**nullable_num},
                "pending_commands": {"type": "integer"},
            },
        },
    }

    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "PLC API — Inovance XYZ Scanner",
            "version": __version__,
            "description": (
                "Управление ПЛК Easy521 на текущей карте регистров (MAIN.LD).\n\n"
                "**Execute отправляет реальные команды на ПЛК.**\n\n"
                "Команды — REST; телеметрия и события — WebSocket `/api/v1/ws`. "
                "Ответ 202 значит «регистры записаны», а не «движение выполнено»."),
        },
        "tags": [
            {"name": "Состояние"}, {"name": "Оси"}, {"name": "Общие"},
            {"name": "WebSocket"},
        ],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        },
    }
    if token_required:
        spec["security"] = [{"bearerAuth": []}]
    return spec
