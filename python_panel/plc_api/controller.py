"""Контроллер ПЛК: единственный поток-владелец Modbus-соединения.

Все обращения к ПЛК — команды и периодический опрос телеметрии — выполняет
один поток ``plc-io`` (синхронный клиент pymodbus не потокобезопасен,
инвариант №8 CLAUDE.md). HTTP/WebSocket-обработчики только ставят команды в
очередь и ждут результата.

* Команды проверяются *до* постановки в очередь: ось существует и подключена,
  не заблокирована концевиком, уставка в пределах мягких границ, запрошенная
  функция реализована текущим ладдером (см. :mod:`plc_api.ladder`).
* Стоп и «стоп всех осей» идут с повышенным приоритетом и обгоняют
  ожидающие команды движения.
* Успешная команда означает «регистры и строб записаны», а не «движение
  выполнено». Факт приезда определяется по телеметрии: событие ``arrived``
  или :meth:`PlcController.wait_arrival`.
* Связь восстанавливается автоматически; команды, ожидающие в очереди в
  момент обрыва, отклоняются (не выполняются позже «вслепую»).
"""
from __future__ import annotations

import itertools
import math
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from plc_panel.codec import float_to_words, floats_to_words, words_to_float
from plc_panel.config import AXES, AXIS_SAFETY, IO, AppSettings, GridLimits

from .ladder import (
    AXIS_NAMES,
    PARAM_SETTLE_MS,
    PULSE_AXES,
    PULSE_ENABLE_SETTLE_MS,
    RESET_STROBE_MS,
    SHARED_ERROR_BLOCK,
    SHARED_ERROR_COUNT,
    STROBE_MS,
    VELOCITY_SETTLE_MS,
    Y0_GAP_MS,
    Y0_STROBE_MS,
    parse_axis,
)
from .transport import ModbusTransport, TransportError

Event = dict
Subscriber = Callable[[Event], None]

_PRIORITY_STOP = 0
_PRIORITY_NORMAL = 1


# ---------------------------------------------------------------------- #
# Ошибки API (HTTP-статус и машиночитаемый код)                          #
# ---------------------------------------------------------------------- #
class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ApiError):
    status, code = 404, "not_found"


class Conflict(ApiError):
    status, code = 409, "conflict"


class MotionAborted(ApiError):
    """Ожидание приезда прервано: стоп, новая команда оси, снятие питания."""
    status, code = 409, "aborted"


class OutOfLimits(ApiError):
    status, code = 422, "out_of_limits"


class Unsupported(ApiError):
    status, code = 422, "unsupported_by_ladder"


class PlcIoError(ApiError):
    status, code = 502, "plc_io_error"


class NotConnected(ApiError):
    status, code = 503, "plc_not_connected"


class WaitTimeout(ApiError):
    status, code = 504, "timeout"


# ---------------------------------------------------------------------- #
@dataclass
class ControllerConfig:
    poll_interval_s: float = 0.15
    reconnect_interval_s: float = 2.0
    poll_failure_limit: int = 3
    command_timeout_s: float = 5.0
    arrival_tolerance_mm: float = 0.4
    active_axes: tuple = tuple(AXES)
    #: Мягкие границы уставок; None — не проверять.
    limits: Optional[GridLimits] = field(default_factory=GridLimits)

    @classmethod
    def from_settings(cls, s: AppSettings, enforce_limits: bool = True) -> "ControllerConfig":
        return cls(
            poll_interval_s=max(0.02, s.poll_interval_ms / 1000.0),
            arrival_tolerance_mm=s.arrival_tolerance_mm,
            active_axes=tuple(s.active_axes),
            limits=s.limits if enforce_limits else None,
        )


@dataclass
class _Job:
    name: str
    axis: Optional[int]
    params: dict
    fn: Callable[[ModbusTransport], None]
    future: Future


def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


def _finite(value: Optional[float]) -> Optional[float]:
    return value if value is not None and math.isfinite(value) else None


class PlcController:
    """Потокобезопасный фасад над ПЛК для HTTP/WebSocket-слоя."""

    def __init__(self, transport: ModbusTransport,
                 config: Optional[ControllerConfig] = None) -> None:
        self._t = transport
        self._cfg = config or ControllerConfig()

        self._jobs: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._connected = False
        self._link_detail = "ещё не подключались"
        self._snapshot: Optional[dict] = None
        self._snapshot_mono = 0.0
        self._arrivals: Dict[int, float] = {}
        # Поколение движения оси: меняется при каждой новой уставке или сбросе
        # (стоп, питание, set_position). Ожидание приезда, начатое в другом
        # поколении, прерывается, а не висит до таймаута.
        self._motion_gen: Dict[int, int] = {n: 0 for n in AXES}
        self._poll_failures = 0
        self._next_connect = 0.0     # monotonic: не переподключаться раньше

        self._subs_lock = threading.Lock()
        self._subscribers: List[Subscriber] = []

    # ------------------------------------------------------------------ #
    # Жизненный цикл и подписки
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="plc-io", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """Остановить поток plc-io и закрыть соединение (не путать со stop(axis))."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._reject_pending(NotConnected("API остановлен"))
        try:
            self._t.close()
        except TransportError:
            pass

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Подписка на события (вызываются из потока plc-io). Возвращает отписку."""
        with self._subs_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._subs_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def _publish(self, event: Event) -> None:
        with self._subs_lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - подписчик не роняет поток ПЛК
                pass

    # ------------------------------------------------------------------ #
    # Состояние
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def status(self) -> dict:
        with self._lock:
            age = (time.monotonic() - self._snapshot_mono) if self._snapshot else None
            return {
                "connected": self._connected,
                "detail": self._link_detail,
                "telemetry_age_s": round(age, 3) if age is not None else None,
                "pending_commands": self._jobs.qsize(),
            }

    def telemetry(self) -> Optional[dict]:
        with self._lock:
            return self._snapshot

    def wait_for_telemetry(self, timeout_s: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while self._snapshot is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._snapshot

    # ------------------------------------------------------------------ #
    # Команды (вызываются из любых потоков)
    # ------------------------------------------------------------------ #
    def power(self, axis, on: bool) -> dict:
        n = self._axis(axis)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            t.write_coil(r.power, on)
            if not on:
                self._clear_arrival(n)
        return self._submit("power", n, {"on": on}, job)

    def reset(self, axis) -> dict:
        n = self._axis(axis)
        r = AXES[n]
        return self._submit("reset", n, {},
                            lambda t: self._pulse(t, r.reset, RESET_STROBE_MS))

    def move_absolute(self, axis, position: float, speed: float,
                      pulse: bool = False) -> dict:
        n = self._axis(axis)
        self._require_number("position", position)
        self._require_number("speed", speed, positive=True)
        if pulse and n not in PULSE_AXES:
            raise Unsupported(
                f"импульс замера по приезду для оси {AXIS_NAMES[n]} в текущем "
                f"ладдере не подключён (доступно: "
                f"{', '.join(AXIS_NAMES[a] for a in sorted(PULSE_AXES))})")
        self._require_motion_allowed(n)
        self._check_limits(n, position)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            if n in PULSE_AXES:
                t.write_coil(r.abs_pulse_en, pulse)
                _sleep_ms(PULSE_ENABLE_SETTLE_MS)
            t.write_registers(r.abs_pos, float_to_words(position))
            t.write_registers(r.abs_spd, float_to_words(speed))
            _sleep_ms(PARAM_SETTLE_MS)
            self._pulse(t, r.abs_exec, STROBE_MS)
            self._set_arrival(n, position)
        return self._submit("move_absolute", n,
                            {"position": position, "speed": speed, "pulse": pulse}, job)

    def move_relative(self, axis, distance: float, speed: float) -> dict:
        n = self._axis(axis)
        self._require_number("distance", distance)
        self._require_number("speed", speed, positive=True)
        self._require_motion_allowed(n)
        current = self._position(n)
        if self._cfg.limits is not None:
            if current is None:
                raise Conflict("нет телеметрии позиции — пределы перемещения "
                               "проверить нельзя")
            self._check_limits(n, current + distance)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            start = self._position(n)
            t.write_registers(r.rel_distance, float_to_words(distance))
            t.write_registers(r.rel_speed, float_to_words(speed))
            _sleep_ms(PARAM_SETTLE_MS)
            self._pulse(t, r.rel_exec, STROBE_MS)
            if start is not None:
                self._set_arrival(n, start + distance)
            else:
                self._clear_arrival(n)
        return self._submit("move_relative", n,
                            {"distance": distance, "speed": speed}, job)

    def move_velocity(self, axis, speed: float, acceleration: float,
                      deceleration: float) -> dict:
        n = self._axis(axis)
        self._require_number("speed", speed)
        if speed == 0:
            raise ApiError("speed не может быть 0 — для остановки используйте stop")
        self._require_number("acceleration", acceleration, positive=True)
        self._require_number("deceleration", deceleration, positive=True)
        self._require_motion_allowed(n)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            self._clear_arrival(n)
            t.write_registers(r.vel_block, floats_to_words(speed, acceleration, deceleration))
            _sleep_ms(VELOCITY_SETTLE_MS)
            self._pulse(t, r.vel_exec, RESET_STROBE_MS)
        return self._submit("move_velocity", n,
                            {"speed": speed, "acceleration": acceleration,
                             "deceleration": deceleration}, job)

    def stop(self, axis) -> dict:
        n = self._axis(axis)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            self._clear_arrival(n)
            self._pulse(t, r.stop_exec, STROBE_MS)
        return self._submit("stop", n, {}, job, priority=_PRIORITY_STOP)

    def stop_all(self, power_off: bool = False) -> dict:
        def job(t: ModbusTransport) -> None:
            for n, r in AXES.items():
                self._clear_arrival(n)
                t.write_coil(r.stop_exec, True)
            _sleep_ms(STROBE_MS)
            for r in AXES.values():
                t.write_coil(r.stop_exec, False)
            if power_off:
                for r in AXES.values():
                    t.write_coil(r.power, False)
        return self._submit("stop_all", None, {"power_off": power_off}, job,
                            priority=_PRIORITY_STOP)

    def set_position(self, axis, position: float) -> dict:
        n = self._axis(axis)
        self._require_number("position", position)
        r = AXES[n]

        def job(t: ModbusTransport) -> None:
            self._clear_arrival(n)
            t.write_registers(r.set_pos_val, float_to_words(position))
            _sleep_ms(PARAM_SETTLE_MS)
            self._pulse(t, r.set_pos_exec, STROBE_MS)
        return self._submit("set_position", n, {"position": position}, job)

    def unblock(self, axis) -> dict:
        n = self._axis(axis)
        s = AXIS_SAFETY[n]
        return self._submit("unblock", n, {},
                            lambda t: self._pulse(t, s.block_reset, RESET_STROBE_MS))

    def y0_pulse(self) -> dict:
        def job(t: ModbusTransport) -> None:
            self._pulse(t, IO.Y0_SET, Y0_STROBE_MS)
            _sleep_ms(Y0_GAP_MS)
            self._pulse(t, IO.Y0_RESET, Y0_STROBE_MS)
        return self._submit("y0_pulse", None, {}, job)

    def y0_set(self, high: bool) -> dict:
        """Статический уровень Y0.

        HIGH удерживает M50 (SET Y0 каждый скан). LOW отпускает M50 и даёт
        *строб* M51: удерживаемый M51 выполнял бы RST Y0 на каждом скане и
        подавлял авто-импульсы замера по приезду (M14 → TPR → Y0).
        """
        def job(t: ModbusTransport) -> None:
            if high:
                t.write_coil(IO.Y0_RESET, False)
                t.write_coil(IO.Y0_SET, True)
            else:
                t.write_coil(IO.Y0_SET, False)
                self._pulse(t, IO.Y0_RESET, Y0_STROBE_MS)
        return self._submit("y0_set", None, {"high": high}, job)

    def wait_arrival(self, axis, target: float, since_mono: float,
                     timeout_s: float, motion_gen: Optional[int] = None) -> dict:
        """Ждать кадр телеметрии (снятый после since_mono) с осью в точке.

        motion_gen — поколение из результата команды движения: если с тех пор
        ось получила стоп/новую команду, ожидание прерывается (MotionAborted).
        """
        n = self._axis(axis)
        name = AXIS_NAMES[n]
        tol = self._cfg.arrival_tolerance_mm
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                if not self._connected:
                    raise NotConnected(f"связь с ПЛК потеряна: {self._link_detail}")
                if motion_gen is not None and self._motion_gen[n] != motion_gen:
                    raise MotionAborted(f"ожидание оси {name} прервано: стоп или "
                                        f"новая команда этой оси")
                snap = self._snapshot
                if snap is not None and self._snapshot_mono >= since_mono:
                    a = snap["axes"][name]
                    if a["blocked"]:
                        raise Conflict(f"ось {name} заблокирована концевиком во "
                                       f"время движения")
                    pos = a["position"]
                    if pos is not None and abs(pos - target) < tol:
                        return a
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pos = snap["axes"][name]["position"] if snap else None
                    raise WaitTimeout(f"ось {name} не пришла в {target} за "
                                      f"{timeout_s} с (позиция: {pos})")
                self._cond.wait(remaining)

    # ------------------------------------------------------------------ #
    # Проверки до постановки в очередь
    # ------------------------------------------------------------------ #
    @staticmethod
    def _axis(axis) -> int:
        n = axis if isinstance(axis, int) and axis in AXES else parse_axis(str(axis))
        if n is None:
            raise NotFound(f"неизвестная ось: {axis!r} (допустимо: "
                           f"{', '.join(AXIS_NAMES.values())})")
        return n

    @staticmethod
    def _require_number(name: str, value, positive: bool = False) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(f"{name} должно быть числом")
        if not math.isfinite(value):
            raise ApiError(f"{name} должно быть конечным числом")
        if positive and value <= 0:
            raise ApiError(f"{name} должно быть больше 0")

    def _require_connected(self) -> None:
        with self._lock:
            if not self._connected:
                raise NotConnected(f"нет связи с ПЛК: {self._link_detail}")

    def _require_motion_allowed(self, n: int) -> None:
        name = AXIS_NAMES[n]
        if n not in self._cfg.active_axes:
            raise Conflict(f"ось {name} не подключена (active_axes)")
        snap = self.telemetry()
        if snap is not None and snap["axes"][name]["blocked"]:
            s = AXIS_SAFETY[n]
            raise Conflict(f"ось {name} заблокирована концевиком (M{s.block_latch}); "
                           f"снимите блокировку: POST /api/v1/axes/{name.lower()}/unblock")

    def _check_limits(self, n: int, target: float) -> None:
        lim = self._cfg.limits
        if lim is None:
            return
        key = AXIS_NAMES[n].lower()
        lo, hi = getattr(lim, f"{key}_min"), getattr(lim, f"{key}_max")
        if not lo <= target <= hi:
            raise OutOfLimits(f"уставка {target} вне пределов оси "
                              f"{AXIS_NAMES[n]}: [{lo}, {hi}]")

    def _position(self, n: int) -> Optional[float]:
        snap = self.telemetry()
        return snap["axes"][AXIS_NAMES[n]]["position"] if snap else None

    # ------------------------------------------------------------------ #
    # Очередь команд
    # ------------------------------------------------------------------ #
    def _submit(self, name: str, axis: Optional[int], params: dict,
                fn: Callable[[ModbusTransport], None],
                priority: int = _PRIORITY_NORMAL) -> dict:
        self._require_connected()
        job = _Job(name, axis, params, fn, Future())
        self._jobs.put((priority, next(self._seq), job))
        try:
            return job.future.result(timeout=self._cfg.command_timeout_s)
        except FutureTimeout:
            job.future.cancel()
            raise WaitTimeout(f"команда {name} не выполнена за "
                              f"{self._cfg.command_timeout_s} с") from None

    def _reject_pending(self, error: ApiError) -> None:
        while True:
            try:
                _, _, job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job.future.set_running_or_notify_cancel():
                job.future.set_exception(error)

    def _execute(self, job: _Job) -> None:
        if not job.future.set_running_or_notify_cancel():
            return      # вызывающий уже отказался ждать — не исполняем
        try:
            job.fn(self._t)
        except TransportError as exc:
            job.future.set_exception(PlcIoError(f"{job.name}: {exc}"))
            if not self._t.is_open():
                self._link_lost(str(exc))
            return
        except ApiError as exc:
            job.future.set_exception(exc)
            return
        except Exception as exc:  # noqa: BLE001 - поток ПЛК не должен умереть
            job.future.set_exception(PlcIoError(f"{job.name}: {exc!r}"))
            return
        with self._lock:
            gen = self._motion_gen.get(job.axis) if job.axis else None
        # issued_at_mono и motion_gen — служебные поля для wait_arrival.
        result = {"command": job.name,
                  "axis": AXIS_NAMES.get(job.axis) if job.axis else None,
                  "params": job.params,
                  "issued_at_mono": time.monotonic(),
                  "motion_gen": gen}
        job.future.set_result(result)
        self._publish({"type": "command", "command": job.name,
                       "axis": result["axis"], "params": job.params})

    @staticmethod
    def _pulse(t: ModbusTransport, coil: int, hold_ms: int) -> None:
        """Строб по переднему фронту: 1 → удержание → 0."""
        t.write_coil(coil, True)
        _sleep_ms(hold_ms)
        t.write_coil(coil, False)

    def _set_arrival(self, n: int, target: float) -> None:
        with self._cond:
            self._arrivals[n] = target
            self._motion_gen[n] += 1
            self._cond.notify_all()

    def _clear_arrival(self, n: int) -> None:
        with self._cond:
            self._arrivals.pop(n, None)
            self._motion_gen[n] += 1
            self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # Поток plc-io: связь, команды, опрос
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            if not self.connected:
                wait = self._next_connect - time.monotonic()
                if wait > 0:            # пауза после обрыва, как у PlcWorker
                    self._stop.wait(wait)
                    continue
                if not self._try_connect():
                    self._stop.wait(self._cfg.reconnect_interval_s)
                    continue
                next_poll = 0.0
            now = time.monotonic()
            if now >= next_poll:
                self._poll()
                next_poll = time.monotonic() + self._cfg.poll_interval_s
                continue
            try:
                _, _, job = self._jobs.get(timeout=next_poll - now)
            except queue.Empty:
                continue
            self._execute(job)

    def _try_connect(self) -> bool:
        try:
            self._t.connect()
        except TransportError as exc:
            self._set_link(False, str(exc))
            return False
        for r in AXES.values():   # MC_ReadAxisError Enable (как PlcWorker)
            try:
                self._t.write_coil(r.read_error_en, True)
            except TransportError:
                pass
        self._poll_failures = 0
        self._set_link(True, "подключено")
        return True

    def _link_lost(self, detail: str) -> None:
        try:
            self._t.close()
        except TransportError:
            pass
        with self._lock:
            self._arrivals.clear()
        self._next_connect = time.monotonic() + self._cfg.reconnect_interval_s
        self._set_link(False, f"связь потеряна: {detail}")
        self._reject_pending(NotConnected(f"связь с ПЛК потеряна: {detail}"))

    def _set_link(self, ok: bool, detail: str) -> None:
        with self._cond:
            changed = ok != self._connected
            self._connected = ok
            self._link_detail = detail
            if not ok:
                self._snapshot = None
            self._cond.notify_all()
        if changed:
            self._publish({"type": "connection", "connected": ok, "detail": detail})

    def _poll(self) -> None:
        try:
            snap = self._read_snapshot()
        except TransportError as exc:
            self._poll_failures += 1
            if not self._t.is_open() or self._poll_failures >= self._cfg.poll_failure_limit:
                self._link_lost(str(exc))
            return
        self._poll_failures = 0

        with self._cond:
            previous = self._snapshot
            self._snapshot = snap
            self._snapshot_mono = time.monotonic()
            arrived = []
            for n, target in list(self._arrivals.items()):
                pos = snap["axes"][AXIS_NAMES[n]]["position"]
                if pos is not None and abs(pos - target) < self._cfg.arrival_tolerance_mm:
                    del self._arrivals[n]
                    arrived.append((n, target, pos))
            self._cond.notify_all()

        self._publish({"type": "telemetry", "data": snap})
        for n, target, pos in arrived:
            self._publish({"type": "arrived", "axis": AXIS_NAMES[n],
                           "target": target, "position": pos})
        for name, a in snap["axes"].items():
            before = previous["axes"][name]["blocked"] if previous else None
            if a["blocked"] is not None and a["blocked"] != before:
                self._publish({"type": "blocked", "axis": name, "blocked": a["blocked"]})

    def _tolerant(self, read: Callable[[], object]):
        """Необязательное чтение: отказ -> None, но обрыв связи пробрасывается."""
        try:
            return read()
        except TransportError:
            if not self._t.is_open():
                raise
            return None

    def _read_snapshot(self) -> dict:
        t = self._t
        block_error = t.read_holding(IO.BLOCK_ERROR_ID, 1)[0]
        errors = t.read_holding(SHARED_ERROR_BLOCK, SHARED_ERROR_COUNT)
        limits = self._tolerant(
            lambda: t.read_discrete(IO.LIMIT_INPUT_BASE, IO.LIMIT_INPUT_COUNT))
        y0 = self._tolerant(lambda: t.read_coils(IO.Y0_SET, 2))

        def limit(addr: int) -> Optional[bool]:
            return limits[addr - IO.LIMIT_INPUT_BASE] if limits is not None else None

        axes = {}
        for n, r in AXES.items():
            s = AXIS_SAFETY[n]
            power = self._tolerant(lambda: t.read_coils(r.power, 1)[0])
            blocked = self._tolerant(lambda: t.read_coils(s.block_latch, 1)[0])
            axes[r.name] = {
                "axis": n,
                "active": n in self._cfg.active_axes,
                "position": _finite(words_to_float(t.read_holding(r.act_pos, 2))),
                "velocity": _finite(words_to_float(t.read_holding(r.act_vel, 2))),
                "power_command": power,
                "blocked": blocked,
                "limit_left": limit(s.limit_left),
                "limit_right": limit(s.limit_right),
            }
        return {
            "timestamp": time.time(),
            "axes": axes,
            "errors": {
                "block_error_id": block_error,
                "servo_error_id": errors[0],
                "axis_error_id": errors[2],
                "shared_between_axes": True,
            },
            "y0": {
                "set_command": y0[0] if y0 else None,
                "reset_command": y0[1] if y0 else None,
            },
        }
