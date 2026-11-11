from aftercare.evaluation.service import require_current_code
from aftercare.queue import worker_app

require_current_code()
app = worker_app()
