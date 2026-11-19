"""Append monotonic request receipt/completion marks; preserve mock behavior."""
import json,os,time
def install(handler):
    original=handler.do_POST
    def wrapped(self):
        if not self.path.endswith('/chat/completions'):return original(self)
        path=os.environ['OVERLAP_MOCK_EVENTS'];started=time.monotonic_ns()
        def emit(kind):
            with open(path,'a') as f:
                f.write(json.dumps({'kind':kind,'request_start_ns':started,'t_ns':time.monotonic_ns()})+'\n');f.flush()
        emit('start')
        try:return original(self)
        finally:emit('end')
    handler.do_POST=wrapped
