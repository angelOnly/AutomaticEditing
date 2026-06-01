netstat -ano | awk '$2 ~ /:(786[0-9]|787[0-9])$/ && $4=="LISTENING" && !seen[$5]++ { system("taskkill //PID " $5 " //T //F") }'
nohup python run_web.py > outputs/run_web.log 2>&1 &
