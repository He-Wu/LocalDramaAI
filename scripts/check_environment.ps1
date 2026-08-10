$ErrorActionPreference = 'Continue'
python --version
git --version
ffmpeg -version | Select-Object -First 2
ollama --version
node --version
npm --version
nvidia-smi --query-gpu=name,memory.used,memory.total,driver_version --format=csv
