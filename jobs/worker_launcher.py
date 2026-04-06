"""
Worker Launcher — reads desired_workers from pipeline_config DB table
and ensures that many queue_worker processes are running.

Called by cron every hour (flock prevents overlap).
Checks if pipeline is paused. Spawns/kills workers as needed.

Usage:
  python /app/jobs/worker_launcher.py
"""
import os
import sys
import subprocess
import signal
import time
import glob

sys.path.insert(0, "/app")

PID_DIR = "/tmp/notino_workers"
WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "queue_worker.py")
PYTHON = sys.executable


def get_pipeline_config() -> dict:
    """Read pipeline_config from DB (singleton row id=1)."""
    from jobs.db import get_conn, put_conn

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT desired_workers, max_workers, paused, pause_reason FROM pipeline_config WHERE id = 1")
            row = cur.fetchone()
            if not row:
                return {"desired_workers": 2, "max_workers": 8, "paused": False, "pause_reason": None}
            return {
                "desired_workers": row[0],
                "max_workers": row[1],
                "paused": row[2],
                "pause_reason": row[3],
            }
    except Exception as e:
        print(f"[launcher] DB error reading pipeline_config: {e}, using defaults", flush=True)
        return {"desired_workers": 2, "max_workers": 8, "paused": False, "pause_reason": None}
    finally:
        put_conn(conn)


def get_running_workers() -> dict[int, int]:
    """Returns {worker_id: pid} for currently running workers."""
    os.makedirs(PID_DIR, exist_ok=True)
    running = {}
    for pid_file in glob.glob(os.path.join(PID_DIR, "worker_*.pid")):
        try:
            worker_id = int(os.path.basename(pid_file).replace("worker_", "").replace(".pid", ""))
            with open(pid_file) as f:
                pid = int(f.read().strip())
            # Check if process is actually alive
            os.kill(pid, 0)
            running[worker_id] = pid
        except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
            # Stale PID file — remove it
            try:
                os.remove(pid_file)
            except FileNotFoundError:
                pass
    return running


def spawn_worker(worker_id: int) -> int:
    """Spawn a new queue_worker process. Returns PID."""
    env = os.environ.copy()
    env["WORKER_ID"] = f"w-{worker_id}"

    proc = subprocess.Popen(
        [PYTHON, WORKER_SCRIPT],
        env=env,
        stdout=open("/proc/1/fd/1", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    pid_file = os.path.join(PID_DIR, f"worker_{worker_id}.pid")
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    return proc.pid


def kill_worker(worker_id: int, pid: int):
    """Gracefully stop a worker (SIGTERM, then SIGKILL after 30s)."""
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"[launcher] Sent SIGTERM to worker w-{worker_id} (pid {pid})", flush=True)
        # Wait up to 30s for graceful shutdown
        for _ in range(30):
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                break
        else:
            # Still alive after 30s — force kill
            os.kill(pid, signal.SIGKILL)
            print(f"[launcher] Force-killed worker w-{worker_id} (pid {pid})", flush=True)
    except ProcessLookupError:
        pass

    pid_file = os.path.join(PID_DIR, f"worker_{worker_id}.pid")
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def main():
    print(f"[launcher] Starting at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    config = get_pipeline_config()
    desired = config["desired_workers"]
    max_w = config["max_workers"]
    paused = config["paused"]

    if paused:
        print(f"[launcher] Pipeline PAUSED: {config['pause_reason'] or 'no reason given'}", flush=True)
        # Kill all running workers
        running = get_running_workers()
        for wid, pid in running.items():
            kill_worker(wid, pid)
        if running:
            print(f"[launcher] Stopped {len(running)} workers", flush=True)
        return

    desired = min(desired, max_w)
    running = get_running_workers()
    current = len(running)

    print(f"[launcher] Config: desired={desired}, max={max_w}, running={current}, workers={list(running.keys())}", flush=True)

    if current == desired:
        print(f"[launcher] Worker count OK ({current}/{desired})", flush=True)
        return

    if current < desired:
        # Spawn missing workers
        existing_ids = set(running.keys())
        for wid in range(1, desired + 1):
            if wid not in existing_ids:
                pid = spawn_worker(wid)
                print(f"[launcher] Spawned worker w-{wid} (pid {pid})", flush=True)

    elif current > desired:
        # Kill excess workers (highest IDs first)
        excess = sorted(running.keys(), reverse=True)
        for wid in excess[:current - desired]:
            kill_worker(wid, running[wid])
            print(f"[launcher] Killed worker w-{wid}", flush=True)

    # Final check
    time.sleep(1)
    final = get_running_workers()
    print(f"[launcher] Done. Running workers: {len(final)} — {list(final.keys())}", flush=True)


if __name__ == "__main__":
    main()
