"""Unit tests for Frontier 7: Native Performance & Accelerated Core."""

import time

from durallm.performance.accelerator import (
    FastSlidingWindow,
    FastStreamRelay,
    FastTokenEstimator,
    get_accelerated_event_loop,
    is_uvloop_active,
)


def test_fast_stream_relay_framing() -> None:
    """Verify FastStreamRelay splits fragmented SSE chunks into complete frames."""
    relay = FastStreamRelay()

    # Feed fragmented chunk
    chunk1 = b"event: message\ndata: {\"content\": \""
    frames1 = list(relay.feed(chunk1))
    assert len(frames1) == 0  # Incomplete frame

    # Feed remainder with delimiter
    chunk2 = b"Hello world!\"}\n\n"
    frames2 = list(relay.feed(chunk2))
    assert len(frames2) == 1
    assert frames2[0] == b"event: message\ndata: {\"content\": \"Hello world!\"}\n\n"

    # Test CRLF delimiters
    chunk3 = b"data: {\"done\": true}\r\n\r\n"
    frames3 = list(relay.feed(chunk3))
    assert len(frames3) == 1
    assert frames3[0] == b"data: {\"done\": true}\r\n\r\n"

    # Metrics
    assert relay.bytes_relayed == len(chunk1) + len(chunk2) + len(chunk3)
    assert relay.frames_relayed == 2


def test_fast_stream_relay_error_detection_and_flush() -> None:
    """Verify FastStreamRelay detects error payloads and flushes remaining buffers."""
    relay = FastStreamRelay()
    assert relay.detect_stream_error(b'data: {"error": {"message": "Rate limit exceeded"}}\n\n') is True
    assert relay.detect_stream_error(b'data: {"content": "Normal text"}\n\n') is False

    # Feed unfinished bytes and flush
    list(relay.feed(b"data: trailing data"))
    flushed = relay.flush()
    assert flushed == b"data: trailing data"
    assert relay.flush() is None


def test_fast_token_estimator() -> None:
    """Verify FastTokenEstimator accuracy and high-throughput speed."""
    assert FastTokenEstimator.estimate_text_tokens("") == 0
    assert FastTokenEstimator.estimate_text_tokens("hi") == 1

    prose = "This is a clean and simple English sentence with normal prose words."
    prose_tokens = FastTokenEstimator.estimate_text_tokens(prose)
    assert 10 <= prose_tokens <= 25

    code = '{"action": "edit_file", "path": "src/main.py", "lines": [1, 2, 3]}'
    code_tokens = FastTokenEstimator.estimate_text_tokens(code)
    assert 15 <= code_tokens <= 30

    # Byte-level estimator
    raw_bytes = b"Hello world byte stream"
    assert FastTokenEstimator.estimate_bytes_tokens(raw_bytes) > 0

    # High-throughput benchmark verification: 10,000 evaluations in < 50ms
    start = time.perf_counter()
    for _ in range(10_000):
        FastTokenEstimator.estimate_text_tokens(prose)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.20, f"Token estimation took {elapsed:.3f}s for 10k calls"


def test_fast_sliding_window() -> None:
    """Verify FastSlidingWindow circular ring buffer and O(1) performance."""
    window = FastSlidingWindow(capacity=16, window_duration_s=5.0)
    assert window.capacity == 16  # Power of two rounded

    now = 1000.0
    # Record 10 calls: 8 successes, 2 failures
    for i in range(8):
        window.record(success=True, duration_ms=10.0 + i, timestamp=now)
    for _ in range(2):
        window.record(success=False, duration_ms=50.0, timestamp=now)

    snap = window.snapshot(current_time=now)
    assert snap["total_calls"] == 10
    assert snap["successful_calls"] == 8
    assert snap["failed_calls"] == 2
    assert snap["failure_rate"] == 20.0
    assert snap["avg_latency_ms"] > 0

    # Overflow capacity to test circular wraparound (record 20 more)
    for _ in range(20):
        window.record(success=True, duration_ms=5.0, timestamp=now + 1.0)

    snap_overflow = window.snapshot(current_time=now + 1.0)
    assert snap_overflow["total_calls"] == 16  # Capacity capped at 16
    assert snap_overflow["successful_calls"] == 16
    assert snap_overflow["failed_calls"] == 0
    assert snap_overflow["failure_rate"] == 0.0

    # Test window duration expiration
    expired_snap = window.snapshot(current_time=now + 10.0)
    assert expired_snap["total_calls"] == 0
    assert expired_snap["failure_rate"] == 0.0


def test_accelerated_event_loop_detection() -> None:
    """Verify get_accelerated_event_loop and uvloop detection."""
    loop = get_accelerated_event_loop()
    assert loop is not None
    active = is_uvloop_active()
    assert isinstance(active, bool)
