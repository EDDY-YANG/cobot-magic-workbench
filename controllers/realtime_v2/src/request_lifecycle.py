"""Typed, owned reference-clock transitions; never authorize STOP recovery."""


class ReferenceCaptureDeferred(RuntimeError):
    def __init__(self, reason, epoch):
        self.reference_capture_deferred = {
            'reason': reason, 'clock_epoch': int(epoch),
            'model_request_started': False, 'physical_dispatch_performed': False}
        super().__init__(reason)


class ReferencePauseInvalidated(RuntimeError):
    def __init__(self, info):
        self.reference_pause_discard = dict(info)
        super().__init__('Request invalidated by an owned MPC reference pause')
