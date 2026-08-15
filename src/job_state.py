# job_state.py
ACTIVE_PROCESS = None

def set_process(process):
    global ACTIVE_PROCESS
    ACTIVE_PROCESS = process

def get_process():
    return ACTIVE_PROCESS