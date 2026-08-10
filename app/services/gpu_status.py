import csv, io, subprocess

class GPUStatusService:
    def read(self):
        try:
            output = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw", "--format=csv,noheader,nounits"], text=True, timeout=10)
            row = next(csv.reader(io.StringIO(output)))
            return {"name": row[0].strip(), "vram_used_mb": float(row[1]), "vram_total_mb": float(row[2]), "utilization_percent": float(row[3]), "temperature_c": float(row[4]), "power_w": float(row[5])}
        except (FileNotFoundError, subprocess.SubprocessError, StopIteration, ValueError):
            return {"status": "unavailable"}
