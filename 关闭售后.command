#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "$(dirname -- "$0")"
./scripts/aftercare.sh stop --vm
result=$?
printf "\n按回车关闭窗口…"
read -r _
exit "$result"
