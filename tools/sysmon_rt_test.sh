#!/usr/bin/env bash
# Short root test of the recorder's real-time raise (about 25 s). Run: sudo bash tools/sysmon_rt_test.sh   (add 12 to run only tests 1 and 2)
# Uses throw-away transient units pinned to the last core; it does not touch the installed service.
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
CORE=$(( $(nproc) - 1 ))
BUSY='import time,os; t=time.time(); 
while time.time()-t<3: pass
c=os.times(); print("cpu used: %.0f%% of the 3 s" % (100*(c.user+c.system)/3))'
echo "== 1. does CPUQuota=15% limit a real-time task? (3 s busy loop, SCHED_RR 10)"
systemd-run --pipe -q --wait --collect -p CPUQuota=15% taskset -c $CORE chrt -r 10 python3 -c "$BUSY"
echo "   (about 15% = the quota works for real-time too; about 95-100% = it does NOT)"
echo
echo "== 2. does LimitRTTIME=1s kill a real-time task that never sleeps? (loop would run 5 s)"
time systemd-run --pipe --wait --collect -p LimitRTTIME=1000000 \
  taskset -c $CORE chrt -r 10 python3 -c "import time; t=time.time()
while time.time()-t<5: pass
print('survived 5 s: the guard did NOT work')" ; echo "   exit: $? (expected: killed after about 1 s)"
echo
[ "${1:-}" = "12" ] && { echo done; exit 0; }
echo "== 3. the recorder itself: forced stall trigger, same limits as the service"
D=$(mktemp -d); printf "dir: $D/out\nkmsg: false\ntriggers: {sched_stall_ms: 0}\nescalate_s: 4\n" > $D/c.yaml
systemd-run -q --collect --unit=aio-sysmon-rttest -p Nice=-5 -p LimitMEMLOCK=infinity -p LimitRTTIME=1000000 -p CPUQuota=15% -p MemoryMax=96M \
  -E PYTHONPATH=/opt/aio-dashboard:/opt/aio-dashboard/lib python3 -m aio_sysmon run --config $D/c.yaml
sleep 3; P=$(systemctl show -p MainPID --value aio-sysmon-rttest)
echo "   scheduler class while raised (RR = real-time):  $(ps -L -o cls= -p $P | sort | uniq -c | tr '\n' ' ')"
sleep 7; echo "   scheduler class 7 s later (TS = back to normal): $(ps -L -o cls= -p $P | sort | uniq -c | tr '\n' ' ')"
systemctl stop aio-sysmon-rttest; sleep 1
PYTHONPATH=/opt/aio-dashboard:/opt/aio-dashboard/lib python3 -m aio_sysmon events --dir $D/out | grep -E "priority|memory_lock"
rm -rf $D; echo "done"
