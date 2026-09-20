import copy
import itertools
import queue
import threading

from concurrent.futures import Future


class RuntimeWorkerStopped(RuntimeError):
    pass


class RuntimeQueueFull(RuntimeError):
    pass


class StaleRuntimeRequest(RuntimeError):
    """
    요청 처리 도중 차량 세션/reset generation이 바뀌어서
    더 이상 현재 고객에게 적용하면 안 되는 결과.
    """
    pass


class _RuntimeTask:
    def __init__(
        self,
        *,
        kind,
        generation,
        request_id=None,
        text=None,
        future=None,
    ):
        self.kind = kind
        self.generation = generation
        self.request_id = request_id
        self.text = text
        self.future = future


class RuntimeWorker:
    """
    DriveThruRuntime 전용 single-owner worker.

    중요한 원칙:
    - DriveThruRuntime 객체는 worker thread 안에서 생성한다.
    - runtime.process()는 worker thread에서만 호출한다.
    - manager.reset()도 worker thread에서만 호출한다.
    - 외부에서는 snapshot만 읽는다.
    """

    def __init__(
        self,
        runtime_factory,
        *,
        max_queue_size=8,
    ):
        self._runtime_factory = runtime_factory

        self._tasks = queue.Queue(
            maxsize=max_queue_size
        )

        self._generation_lock = threading.Lock()
        self._generation = 0

        self._snapshot_lock = threading.Lock()
        self._snapshot = {
            "state": {},
            "pending": None,
            "generation": 0,
            "busy": False,
        }

        self._request_counter = itertools.count(1)

        self._ready = threading.Event()
        self._stopped = threading.Event()

        self._thread = threading.Thread(
            target=self._run,
            name="soomac-runtime-worker",
            daemon=True,
        )

        self._thread.start()

        if not self._ready.wait(timeout=5.0):
            raise RuntimeError(
                "RuntimeWorker 초기화 시간이 초과되었습니다."
            )

    # ========================================================
    # GENERATION
    # ========================================================

    @property
    def generation(self):
        with self._generation_lock:
            return self._generation

    # ========================================================
    # SNAPSHOT
    # ========================================================

    def snapshot(self):
        """
        Runtime 객체 자체는 외부에 노출하지 않는다.

        마지막으로 정상 commit/reset된 상태의 복사본만 반환한다.
        """
        with self._snapshot_lock:
            return copy.deepcopy(
                self._snapshot
            )

    def _update_snapshot(
        self,
        runtime,
        *,
        busy=None,
    ):
        with self._snapshot_lock:

            current_busy = (
                self._snapshot["busy"]
                if busy is None
                else bool(busy)
            )

            self._snapshot = {
                "state": copy.deepcopy(
                    runtime.manager.state
                ),
                "pending": copy.deepcopy(
                    runtime.manager.pending
                ),
                "generation": self.generation,
                "busy": current_busy,
            }

    def _set_busy(self, busy):
        with self._snapshot_lock:
            self._snapshot["busy"] = bool(
                busy
            )

    # ========================================================
    # PROCESS
    # ========================================================

    def submit(
        self,
        text,
    ):
        if self._stopped.is_set():
            raise RuntimeWorkerStopped(
                "RuntimeWorker가 이미 종료되었습니다."
            )

        future = Future()

        request_id = next(
            self._request_counter
        )

        generation = self.generation

        task = _RuntimeTask(
            kind="process",
            generation=generation,
            request_id=request_id,
            text=text,
            future=future,
        )

        try:
            self._tasks.put_nowait(
                task
            )

        except queue.Full:
            raise RuntimeQueueFull(
                "Runtime 요청 queue가 가득 찼습니다."
            )

        return (
            request_id,
            generation,
            future,
        )

    def process(
        self,
        text,
        *,
        timeout=None,
    ):
        """
        기존 app 호환용 synchronous wrapper.

        호출한 main thread는 결과를 기다리지만,
        실제 runtime.process()는 전용 worker thread에서 실행된다.

        추후 STT producer thread는 이 시간에도 계속 음성을 받을 수 있다.
        """

        _, _, future = self.submit(
            text
        )

        return future.result(
            timeout=timeout
        )

    # ========================================================
    # RESET / SESSION INVALIDATION
    # ========================================================

    def invalidate_and_reset(
        self,
        *,
        wait=False,
        timeout=None,
    ):
        """
        현재 세션 generation을 즉시 무효화하고
        reset을 worker queue에 넣는다.

        실행 중이던 LLM 요청이 나중에 끝나더라도
        generation이 달라졌다면 그 결과는 폐기된다.
        """

        if self._stopped.is_set():
            raise RuntimeWorkerStopped(
                "RuntimeWorker가 이미 종료되었습니다."
            )

        with self._generation_lock:

            self._generation += 1

            generation = (
                self._generation
            )

            future = Future()

            task = _RuntimeTask(
                kind="reset",
                generation=generation,
                future=future,
            )

            try:
                self._tasks.put(
                    task,
                    timeout=1.0,
                )

            except queue.Full:
                raise RuntimeQueueFull(
                    "Runtime reset queue 등록에 실패했습니다."
                )

        if wait:
            future.result(
                timeout=timeout
            )

        return future

    # ========================================================
    # STOP
    # ========================================================

    def stop(
        self,
        *,
        timeout=5.0,
    ):
        if self._stopped.is_set():
            return

        future = Future()

        task = _RuntimeTask(
            kind="stop",
            generation=self.generation,
            future=future,
        )

        try:
            self._tasks.put(
                task,
                timeout=1.0,
            )

        except queue.Full:
            return

        try:
            future.result(
                timeout=timeout
            )
        except Exception:
            pass

        self._thread.join(
            timeout=timeout
        )

    # ========================================================
    # WORKER LOOP
    # ========================================================

    def _run(self):

        runtime = self._runtime_factory()

        self._update_snapshot(
            runtime,
            busy=False,
        )

        self._ready.set()

        try:

            while True:

                task = self._tasks.get()

                try:

                    # ==========================================
                    # STOP
                    # ==========================================

                    if task.kind == "stop":

                        if (
                            task.future is not None
                            and not task.future.done()
                        ):
                            task.future.set_result(
                                True
                            )

                        break

                    # ==========================================
                    # RESET
                    # ==========================================

                    if task.kind == "reset":

                        runtime.manager.reset()

                        self._update_snapshot(
                            runtime,
                            busy=False,
                        )

                        if (
                            task.future is not None
                            and not task.future.done()
                        ):
                            task.future.set_result(
                                True
                            )

                        continue

                    # ==========================================
                    # PROCESS
                    # ==========================================

                    if task.kind == "process":

                        # queue에서 기다리는 동안
                        # 세션이 이미 바뀌었으면 LLM 호출 자체를 하지 않는다.
                        if (
                            task.generation
                            != self.generation
                        ):

                            if not task.future.done():
                                task.future.set_exception(
                                    StaleRuntimeRequest(
                                        "요청 처리 전에 "
                                        "고객 세션이 변경되었습니다."
                                    )
                                )

                            continue

                        self._set_busy(
                            True
                        )

                        try:

                            result = runtime.process(
                                task.text
                            )

                        except Exception as e:

                            self._update_snapshot(
                                runtime,
                                busy=False,
                            )

                            if not task.future.done():
                                task.future.set_exception(
                                    e
                                )

                            continue

                        # LLM 추론 중 차량 이탈/reset 등이 발생한 경우.
                        #
                        # runtime 내부 처리는 끝났어도
                        # 현재 고객에게 이 결과를 적용하면 안 된다.
                        if (
                            task.generation
                            != self.generation
                        ):

                            self._update_snapshot(
                                runtime,
                                busy=False,
                            )

                            if not task.future.done():
                                task.future.set_exception(
                                    StaleRuntimeRequest(
                                        "요청 처리 중 "
                                        "고객 세션이 변경되었습니다."
                                    )
                                )

                            continue

                        self._update_snapshot(
                            runtime,
                            busy=False,
                        )

                        if not task.future.done():
                            task.future.set_result(
                                result
                            )

                        continue

                    raise RuntimeError(
                        f"알 수 없는 Runtime task: "
                        f"{task.kind}"
                    )

                finally:
                    self._tasks.task_done()

        finally:

            self._stopped.set()

            with self._snapshot_lock:
                self._snapshot["busy"] = False
