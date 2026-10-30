import json,sys,tempfile,time
from pathlib import Path
from looplab.serve.agent_events import append_events,read_outbox
with tempfile.TemporaryDirectory(prefix='looplab-outbox-cpu-') as tmp:
    start=time.perf_counter()
    for i in range(300):
        append_events(Path(tmp), [{'key':str(i), 'run_id':'fixture','run_uid':'u','kind':'node_evaluated','summary':'result'}])
    append_ms=(time.perf_counter()-start)*1000
    start=time.perf_counter(); page=read_outbox(Path(tmp),after=250,limit=50); read_ms=(time.perf_counter()-start)*1000
    print(json.dumps({'variant':sys.argv[1], 'events':300, 'append_ms':append_ms, 'read_ms':read_ms, 'read_rows':len(page['events'])}))
