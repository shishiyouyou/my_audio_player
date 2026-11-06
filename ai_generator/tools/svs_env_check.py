from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_ROOT = Path(r"D:\music_svs")


def _gb(value: int | float) -> float:
    return round(float(value) / 1024 / 1024 / 1024, 2)


def _run(command: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        output = (result.stdout or result.stderr or "").strip()
        return result.returncode, output
    except Exception as exc:
        return 1, str(exc)


def _probe_openvpi(root: Path, python_exe: Path) -> dict:
    runtime_dir = root / "runtime" / "OpenVPI-DiffSinger"
    infer_script = runtime_dir / "scripts" / "infer.py"
    data = {
        "path": str(runtime_dir),
        "exists": runtime_dir.exists(),
        "inferScript": infer_script.exists(),
        "inferCli": False,
    }
    if not infer_script.exists():
        return data
    code, output = _run([str(python_exe), str(infer_script), "--help"], timeout=20)
    data["inferCli"] = code == 0 and "Commands:" in output
    data["probe"] = output[:300]
    return data


def check(root: Path = DEFAULT_ROOT) -> dict:
    root = root.resolve()
    svs_python = root / ".venv" / "Scripts" / "python.exe"
    probe_python = svs_python if svs_python.exists() else Path(sys.executable)
    disk = shutil.disk_usage(root if root.exists() else root.parent)
    status: dict = {
        "root": str(root),
        "rootExists": root.exists(),
        "disk": {
            "totalGb": _gb(disk.total),
            "freeGb": _gb(disk.free),
            "ok": disk.free >= 60 * 1024**3,
            "recommendedFreeGb": 60,
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "svsPython": {
            "executable": str(probe_python),
            "exists": probe_python.exists(),
        },
        "ffmpeg": shutil.which("ffmpeg") or "",
        "nvidiaSmi": {},
        "torch": {},
        "runtime": {},
        "paths": {},
        "ready": False,
        "missing": [],
        "warnings": [],
    }

    code, output = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if code == 0 and output:
        parts = [item.strip() for item in output.splitlines()[0].split(",")]
        if len(parts) >= 4:
            status["nvidiaSmi"] = {
                "available": True,
                "name": parts[0],
                "totalMb": int(float(parts[1])),
                "freeMb": int(float(parts[2])),
                "driver": parts[3],
                "freeOk": int(float(parts[2])) >= 8192,
            }
    else:
        status["nvidiaSmi"] = {"available": False, "error": output}

    torch_probe = (
        "import json\n"
        "try:\n"
        " import torch\n"
        " data={'installed':True,'version':getattr(torch,'__version__',''),'cudaAvailable':bool(torch.cuda.is_available())}\n"
        " data['device']=torch.cuda.get_device_name(0) if data['cudaAvailable'] else ''\n"
        " print(json.dumps(data))\n"
        "except Exception as exc:\n"
        " print(json.dumps({'installed':False,'error':str(exc)}))\n"
    )
    code, output = _run([str(probe_python), "-c", torch_probe], timeout=30)
    try:
        status["torch"] = json.loads(output.splitlines()[-1])
    except Exception:
        status["torch"] = {"installed": False, "error": output}
    status["runtime"] = _probe_openvpi(root, probe_python)

    expected_paths = {
        "models": root / "models",
        "runtime": root / "runtime",
        "outputs": root / "outputs",
        "logs": root / "logs",
        "openvpiRuntime": root / "runtime" / "OpenVPI-DiffSinger",
        "diffSingerCheckpoint": root / "models" / "OpenVPI-DiffSinger",
        "legacyDiffSingerCheckpoint": root / "models" / "DiffSinger",
        "rvc": root / "models" / "RVC",
    }
    for key, path in expected_paths.items():
        status["paths"][key] = {"path": str(path), "exists": path.exists()}

    if not status["ffmpeg"]:
        status["missing"].append("ffmpeg")
    if not status["nvidiaSmi"].get("available"):
        status["missing"].append("nvidia-smi/GPU")
    elif not status["nvidiaSmi"].get("freeOk"):
        status["warnings"].append("GPU free VRAM is below 8GB; close Comfy/other GPU apps before SVS inference")
    if not status["torch"].get("installed"):
        status["missing"].append("torch")
    elif not status["torch"].get("cudaAvailable"):
        status["missing"].append("CUDA-enabled PyTorch")
    if not status["disk"]["ok"]:
        status["warnings"].append("SVS disk free space is below 60GB")
    if not status["runtime"].get("exists"):
        status["missing"].append("OpenVPI DiffSinger runtime")
    elif not status["runtime"].get("inferCli"):
        status["missing"].append("OpenVPI DiffSinger inference CLI")
    if not (status["paths"]["diffSingerCheckpoint"]["exists"] or status["paths"]["legacyDiffSingerCheckpoint"]["exists"]):
        status["missing"].append("OpenVPI DiffSinger checkpoint")

    status["ready"] = not any(
        item in status["missing"]
        for item in (
            "ffmpeg",
            "nvidia-smi/GPU",
            "CUDA-enabled PyTorch",
            "OpenVPI DiffSinger runtime",
            "OpenVPI DiffSinger inference CLI",
            "OpenVPI DiffSinger checkpoint",
        )
    )
    return status


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    print(json.dumps(check(root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
