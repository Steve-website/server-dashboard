import os
import time
import threading
import collections

import paramiko
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# Connection config (override via environment / .env)
# ---------------------------------------------------------------------------
SSH_HOST = os.getenv("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASSWORD = os.getenv("SSH_PASSWORD", "")
COLLECT_INTERVAL = float(os.getenv("COLLECT_INTERVAL", "5"))
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "240"))

# ---------------------------------------------------------------------------
# Collector script executed ON the target Linux server over SSH.
# Prints a flat KEY/VALUE stream that parse_metrics() turns into a dict.
# CPU and network rates use two 1-second samples for accurate percentages/rates.
# ---------------------------------------------------------------------------
REMOTE_SCRIPT = r"""
#!/bin/bash
set +e

# ---------- CPU: two samples 1s apart, all math in awk ----------
awk '/^cpu/{print}' /proc/stat > /tmp/ds_cpu_a
declare -A RX1 TX1
while IFS= read -r line; do
  case "$line" in *:*) ;; *) continue;; esac
  iface="${line%%:*}"; iface="$(printf '%s' "$iface" | tr -d ' ')"
  rest="${line#*:}"
  RX1["$iface"]="$(awk '{print $1}' <<<"$rest")"
  TX1["$iface"]="$(awk '{print $9}' <<<"$rest")"
done < <(sed -n '3,$p' /proc/net/dev)

sleep 1

awk '/^cpu/{print}' /proc/stat > /tmp/ds_cpu_b

awk '
FNR==NR { tot[$1]=$2+$3+$4+$5+$6+$7+$8+$9+$10; idle[$1]=$5+$6; next }
{
  name=$1; t=$2+$3+$4+$5+$6+$7+$8+$9+$10; id=$5+$6;
  dt=t-tot[name]; di=id-idle[name];
  if (dt<=0) p=0; else p=100*(1-di/dt);
  if (name=="cpu") printf "CPU_TOTAL %.1f\n", p;
  else printf "CPU_CORE %s %.1f\n", substr(name,4), p;
}
' /tmp/ds_cpu_a /tmp/ds_cpu_b

# ---------- Network (cumulative bytes + bytes/sec rate) ----------
while IFS= read -r line; do
  case "$line" in *:*) ;; *) continue;; esac
  iface="${line%%:*}"; iface="$(printf '%s' "$iface" | tr -d ' ')"
  rest="${line#*:}"
  rx="$(awk '{print $1}' <<<"$rest")"
  tx="$(awk '{print $9}' <<<"$rest")"
  rxr="$(awk "BEGIN{v=($rx - ${RX1[$iface]:-0}); if(v<0)v=0; printf \"%.0f\", v}")"
  txr="$(awk "BEGIN{v=($tx - ${TX1[$iface]:-0}); if(v<0)v=0; printf \"%.0f\", v}")"
  echo "NET $iface $rx $tx $rxr $txr"
done < <(sed -n '2,$p' /proc/net/dev)

# ---------- Memory ----------
eval "$(free -k | awk '/^Mem:/{printf "MEM_TOTAL=%s\n",$2; printf "MEM_USED=%s\n",$3; printf "MEM_FREE=%s\n",$4; printf "MEM_SHARED=%s\n",$5; printf "MEM_CACHE=%s\n",$6; printf "MEM_AVAILABLE=%s\n",$7} /^Swap:/{printf "SWAP_TOTAL=%s\n",$2; printf "SWAP_USED=%s\n",$3; printf "SWAP_FREE=%s\n",$4}')"
echo "MEM_TOTAL ${MEM_TOTAL:-0}"
echo "MEM_USED ${MEM_USED:-0}"
echo "MEM_FREE ${MEM_FREE:-0}"
echo "MEM_SHARED ${MEM_SHARED:-0}"
echo "MEM_CACHE ${MEM_CACHE:-0}"
echo "MEM_AVAILABLE ${MEM_AVAILABLE:-0}"
echo "SWAP_TOTAL ${SWAP_TOTAL:-0}"
echo "SWAP_USED ${SWAP_USED:-0}"
echo "SWAP_FREE ${SWAP_FREE:-0}"

# ---------- Load / uptime / counts ----------
read l1 l5 l15 _rest < /proc/loadavg
echo "LOAD $l1 $l5 $l15"
echo "UPTIME $(awk '{printf "%d", $1}' /proc/uptime)"
echo "NCPU $(nproc)"
echo "NPROCS $(ls /proc 2>/dev/null | grep -E '^[0-9]+$' | wc -l | tr -d ' ')"

# ---------- GPU (nvidia-smi) ----------
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,memory.free --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r gidx gname gtemp gutil gused gtotal gfree; do
    [ -z "$gidx" ] && continue
    echo "GPU_NAME $gidx $gname"
    echo "GPU_TEMP $gidx $(awk "BEGIN{printf \"%.1f\", $gtemp}")"
    echo "GPU_UTIL $gidx $(awk "BEGIN{printf \"%.1f\", $gutil}")"
    echo "GPU_MEM_USED $gidx $gused"
    echo "GPU_MEM_TOTAL $gidx $gtotal"
    echo "GPU_MEM_FREE $gidx $gfree"
  done
  nvidia-smi --query-compute-apps=gpu_index,pid,used_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r gidx gpid gmem; do
    [ -z "$gpid" ] && continue
    echo "GPU_PROC $gidx $gpid $gmem"
  done
else
  echo "GPU_NONE 1"
fi

# ---------- Temperatures (thermal_zone + hwmon) ----------
for z in /sys/class/thermal/thermal_zone*; do
  [ -e "$z" ] || continue
  t=$(cat "$z/type" 2>/dev/null)
  v=$(cat "$z/temp" 2>/dev/null)
  if [ "$t" = "x86_pkg_temp" ] && [ -n "$v" ]; then
    echo "TEMP CPU_PACKAGE x86_pkg_temp $(awk "BEGIN{printf \"%.1f\", $v/1000}")"
  fi
done
for h in /sys/class/hwmon/hwmon*; do
  [ -e "$h" ] || continue
  nm=$(cat "$h/name" 2>/dev/null)
  for t in "$h"/temp*_input; do
    [ -e "$t" ] || continue
    v=$(cat "$t" 2>/dev/null)
    [ -z "$v" ] && continue
    lbl=$(cat "${t%_input}_label" 2>/dev/null)
    [ -z "$lbl" ] && lbl="sensor"
    echo "TEMP $(printf '%s' "$nm" | tr ' ' '_') $(printf '%s' "$lbl" | tr ' ' '_') $(awk "BEGIN{printf \"%.1f\", $v/1000}")"
  done
done

# ---------- Disk (real mounts only) ----------
df -BM -x tmpfs -x devtmpfs -x squashfs -x efivarfs 2>/dev/null | tail -n +2 | while read -r fs size used avail pcent mount; do
  case "$size" in *[M]) ;; *) continue;; esac
  size="${size%M}"; used="${used%M}"; pcent="${pcent%\%}"
  echo "DISK $mount $size $used $pcent"
done

# ---------- Top processes ----------
ps aux --sort=-%cpu --no-headers | head -n 5 | awk '{cmd=""; for(i=11;i<=NF;i++) cmd=cmd (i>11?" ":"") $i; print $3"|"$4"|"cmd}' | while IFS= read -r l; do echo "PROCS_CPU $l"; done
ps aux --sort=-%mem --no-headers | head -n 5 | awk '{cmd=""; for(i=11;i<=NF;i++) cmd=cmd (i>11?" ":"") $i; print $4"|"$3"|"cmd}' | while IFS= read -r l; do echo "PROCS_MEM $l"; done
"""


def _gpu_blank(idx) -> dict:
    return {"idx": str(idx), "name": "", "temp": 0.0, "util": 0.0, "mem_used": 0, "mem_total": 0, "mem_free": 0}


def parse_metrics(out: str) -> dict:
    d = {
        "cpu_total": 0.0, "cores": {}, "ncpu": 0, "nprocs": 0,
        "load": [0.0, 0.0, 0.0], "uptime": 0,
        "mem_total": 0, "mem_used": 0, "mem_free": 0, "mem_shared": 0,
        "mem_cache": 0, "mem_available": 0,
        "swap_total": 0, "swap_used": 0, "swap_free": 0,
        "net": [], "disks": [], "procs_cpu": [], "procs_mem": [],
        "gpu": [], "gpu_procs": [], "temps": [],
        "gpu_temp": 0.0, "gpu_util": 0.0,
        "gpu_mem_used": 0, "gpu_mem_total": 0, "gpu_mem_free": 0,
    }
    gpus = {}
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        p = line.split()
        k = p[0]
        if k == "CPU_TOTAL":
            d["cpu_total"] = float(p[1])
        elif k == "CPU_CORE":
            d["cores"][p[1]] = float(p[2])
        elif k == "NCPU":
            d["ncpu"] = int(p[1])
        elif k == "NPROCS":
            d["nprocs"] = int(p[1])
        elif k == "LOAD":
            d["load"] = [float(p[1]), float(p[2]), float(p[3])]
        elif k == "UPTIME":
            d["uptime"] = int(p[1])
        elif k.startswith("MEM_"):
            d[k.lower()] = int(p[1])
        elif k.startswith("SWAP_"):
            d[k.lower()] = int(p[1])
        elif k == "NET":
            d["net"].append({
                "iface": p[1], "rx": int(p[2]), "tx": int(p[3]),
                "rx_rate": int(p[4]), "tx_rate": int(p[5]),
            })
        elif k == "DISK":
            d["disks"].append({
                "mount": p[1], "size": int(p[2]), "used": int(p[3]), "pct": float(p[4]),
            })
        elif k == "PROCS_CPU":
            parts = p[1].split("|")
            if len(parts) == 3:
                d["procs_cpu"].append(
                    {"cpu": float(parts[0]), "mem": float(parts[1]), "name": parts[2]}
                )
        elif k == "PROCS_MEM":
            parts = p[1].split("|")
            if len(parts) == 3:
                d["procs_mem"].append(
                    {"mem": float(parts[0]), "cpu": float(parts[1]), "name": parts[2]}
                )
        elif k == "GPU_NAME":
            idx = p[1]
            gpus.setdefault(idx, _gpu_blank(idx))["name"] = " ".join(p[2:])
        elif k == "GPU_TEMP":
            gpus.setdefault(p[1], _gpu_blank(p[1]))["temp"] = float(p[2])
        elif k == "GPU_UTIL":
            gpus.setdefault(p[1], _gpu_blank(p[1]))["util"] = float(p[2])
        elif k == "GPU_MEM_USED":
            gpus.setdefault(p[1], _gpu_blank(p[1]))["mem_used"] = int(p[2])
        elif k == "GPU_MEM_TOTAL":
            gpus.setdefault(p[1], _gpu_blank(p[1]))["mem_total"] = int(p[2])
        elif k == "GPU_MEM_FREE":
            gpus.setdefault(p[1], _gpu_blank(p[1]))["mem_free"] = int(p[2])
        elif k == "GPU_PROC":
            d["gpu_procs"].append({"idx": p[1], "pid": p[2], "mem": p[3] if len(p) > 3 else "0"})
        elif k == "GPU_NONE":
            pass
        elif k == "TEMP":
            if len(p) >= 4:
                try:
                    d["temps"].append(
                        {"source": p[1], "label": p[2], "c": float(p[3])}
                    )
                except ValueError:
                    pass

    d["gpu"] = sorted(gpus.values(), key=lambda x: int(x["idx"]))
    if d["gpu"]:
        f = d["gpu"][0]
        d["gpu_temp"] = f["temp"]
        d["gpu_util"] = f["util"]
        d["gpu_mem_used"] = f["mem_used"]
        d["gpu_mem_total"] = f["mem_total"]
        d["gpu_mem_free"] = f["mem_free"]
    if d["mem_total"] > 0:
        d["mem_used_pct"] = round(d["mem_used"] / d["mem_total"] * 100, 1)
        d["mem_available_pct"] = round(d["mem_available"] / d["mem_total"] * 100, 1)
    else:
        d["mem_used_pct"] = d["mem_available_pct"] = 0.0
    if d["swap_total"] > 0:
        d["swap_used_pct"] = round(d["swap_used"] / d["swap_total"] * 100, 1)
    else:
        d["swap_used_pct"] = 0.0
    return d


def collect() -> dict:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            SSH_HOST, port=SSH_PORT, username=SSH_USER,
            password=SSH_PASSWORD, timeout=10, banner_timeout=10,
        )
        stdin, stdout, stderr = client.exec_command("bash")
        stdin.write(REMOTE_SCRIPT + "\n")
        stdin.close()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        client.close()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"SSH to {SSH_USER}@{SSH_HOST}:{SSH_PORT} failed: {e}")
    try:
        parsed = parse_metrics(out)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"parse error: {e}")
    if err.strip():
        parsed["_stderr"] = err.strip()[:500]
    return parsed


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
history_buffer = collections.deque(maxlen=HISTORY_MAX)
_lock = threading.Lock()
latest = {"error": "initializing..."}


def collect_loop() -> None:
    global latest
    while True:
        try:
            data = collect()
            data["ts"] = int(time.time() * 1000)
            data["error"] = None
            latest = data
            with _lock:
                history_buffer.append(dict(data))
            print(
                f"collected cpu={data.get('cpu_total')}% "
                f"cores={len(data.get('cores', {}))} "
                f"mem={data.get('mem_used_pct')}% "
                f"disks={len(data.get('disks', []))} "
                f"gpus={len(data.get('gpu', []))}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"collect error: {e}", flush=True)
            latest = {"error": str(e), "ts": int(time.time() * 1000)}
        time.sleep(COLLECT_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=collect_loop, daemon=True).start()
    yield


app = FastAPI(title="Server Dashboard", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "templates", "index.html"), "r", encoding="utf-8") as _f:
    HTML_FILE = _f.read()


@app.get("/api/metrics")
def metrics():
    return latest


@app.get("/api/history")
def history(points: int = 90):
    with _lock:
        return list(history_buffer)[-max(1, points):]


@app.get("/api/config")
def config():
    return {"host": SSH_HOST, "user": SSH_USER, "port": SSH_PORT, "interval": COLLECT_INTERVAL}


@app.get("/health")
def health():
    return {"status": "ok", "host": SSH_HOST, "last_error": latest.get("error")}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_FILE
