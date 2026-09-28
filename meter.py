"""Log input level (dBFS, DC removed) every 50 ms: python3 meter.py [seconds]"""
import array, math, subprocess, sys
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 45
rate, chunk = 16000, 800
p = subprocess.Popen(["parecord", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1",
                      "--latency-msec=20"], stdout=subprocess.PIPE)
n = 0
while n < secs * rate:
    a = array.array("h", p.stdout.read(chunk * 2))
    n += len(a)
    m = sum(a) / len(a)
    rms = math.sqrt(sum((x - m) ** 2 for x in a) / len(a))
    db = 20 * math.log10(max(rms, 1) / 32768)
    print(f"{n / rate:6.2f}s {db:7.1f} dB " + "#" * max(0, int((db + 80) / 2)), flush=True)
p.terminate()
