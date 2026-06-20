# Inovance XYZ Antenna Scanner Panel — PyQt5

Python/PyQt5 port of the original Tauri (Rust + JS) control panel. It drives a
three-axis (X/Y/Z) antenna scanner via **Modbus TCP** and an external analyzer
via **SCPI over TCP**.

## Features

- Live telemetry polling (positions, velocities, error codes, discrete I/O).
- Manual motion: power, reset, absolute move, velocity/cruise, stop, set/zero
  position — per axis.
- Easy521 discrete I/O: Y0 trigger pulse, Y1 static level, Y2 frequency generator,
  X0–X3 input monitoring.
- Automatic scan **scenario runner** with polymorphic steps (move, move+pulse,
  delay, SCPI, hardware pulse), reordering, JSON import/export.
- XY-plane plot and per-axis linear gauges (custom `QPainter` widgets).
- **Auto-reconnect**: the Modbus TCP link is re-established automatically after a
  drop or after a VPN turns on / switches; the connect button shows
  «Переподключение…» while retrying. On reconnect the durable queue reconciles
  any in-flight move.
- **Dockable journal**: the PLC system journal is a dock widget — docked to the
  side by default, but movable, floatable and closable (toggle with «Журнал»);
  mirrored to `‹temp›/plc_panel_log.txt`.

## Run

```bash
pip install -r requirements.txt
python main.py          # or:  python -m plc_panel
```

## Architecture

| Module | Responsibility |
|--------|----------------|
| `config.py` | Register map (mirrors the original `config.rs`) and settings |
| `codec.py` | IEEE-754 ↔ Modbus word (REAL) conversion |
| `models.py` | `Telemetry`, `ScanStep` data structures |
| `modbus_worker.py` | All Modbus I/O, on a dedicated worker thread |
| `service.py` | Thread-safe, signal-based facade used by the UI |
| `queue_backbone.py` | Durable command queue — integrates `core/` (servo_core) |
| `scenario.py` | Scan-sequence state machine over the durable queue |
| `scpi.py` | SCPI-over-TCP helper |
| `ui/` | `MainWindow`, charts, reusable axis panel, queue widget |

All PLC communication happens on a single `QThread`; the GUI thread only emits
request signals and renders results, so the interface never blocks on the
network.

## Durable command queue (servo_core integration)

The scan scenario is backed by **servo_core** (the repo-root `core/` package):
its single-file WAL SQLite with a transactional `queue + history + dead-letter`,
a monotonic per-channel `seq`, and crash recovery. `queue_backbone.MotionQueue`
adapts it to this app; `scenario.ScenarioRunner` is the executor.

What is and isn't reused — servo_core's *protocol* assumes a handshake PLC
(`ack_seq` / `command_state` mirror registers) that **`MAIN.LD` does not
implement**, so its `MotionExecutor` / `PymodbusSerialDriver` are **not** used.
Execution stays on the real `PlcWorker` (strobe coils + telemetry). The runner
maps servo_core's invariants onto this hardware:

- **Transactional integrity** — claim/complete/dead-letter go through
  `CommandRepository` (queue + history updated in one transaction).
- **Ack by fact** — a move is marked `DONE` only when telemetry shows the axis
  has reached the target (within `arrival_tolerance_mm`), not when the strobe
  was written.
- **Absolute-only / idempotent** — only `MOVE` / `MOVE_PULSE` (absolute) are
  persisted; re-issuing after a crash never accumulates offset.
- **Crash recovery, E-stop gated** — on every (re)connect the runner reconciles
  any `IN_FLIGHT` move against live telemetry: already-at-target completes
  without moving; otherwise the absolute move is re-issued. While the X3 E-stop
  input is asserted, recovery waits for `acknowledge_safe_state()`.
- **Retry / dead-letter** — a move that doesn't arrive within `move_timeout_ms`
  is retried up to `max_move_attempts`, then dead-lettered (aborting the run).

Orchestration steps (delay / SCPI / pulse) are sequencer concerns and run
inline. The queue file defaults to `python_panel/scan_queue.db`
(`AppSettings.queue_db_path`).

Verify the persistence/recovery contract headless (no Qt, no hardware):

```bash
python -m plc_panel.queue_demo   # ends on the effectively-once line
```
