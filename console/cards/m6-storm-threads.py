# SOURCE: scripts/steps/m6_storm.py L86-87
ths = [threading.Thread(target=worker,
           args=(maya, WORKERS / RPS, False))
       for _ in range(WORKERS)]
ths.append(threading.Thread(target=worker,
           args=(leo, 1.0, True)))
