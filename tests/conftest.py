"""Shared test fixtures for building SnooData/SnooStateMachine objects."""

from python_snoo.containers import SnooData, SnooEvents, SnooStateMachine, SnooStates


def make_state_machine(
    session_id: str = "1",
    state: str = "BASELINE",
    since_session_start_ms: int = 0,
) -> SnooStateMachine:
    return SnooStateMachine(
        up_transition="NONE",
        since_session_start_ms=since_session_start_ms,
        sticky_white_noise="0",
        weaning="0",
        time_left=-1,
        session_id=session_id,
        state=SnooStates(state),
        is_active_session=bool(session_id not in ("0", "")),
        down_transition="NONE",
        hold="0",
        audio="0",
    )


def make_event(
    session_id: str = "1",
    state: str = "BASELINE",
    event_time_ms: int = 0,
    since_session_start_ms: int = 0,
    left_safety_clip: int = 1,
    right_safety_clip: int = 1,
    event: SnooEvents = SnooEvents.ACTIVITY,
) -> SnooData:
    return SnooData(
        left_safety_clip=left_safety_clip,
        rx_signal={},
        right_safety_clip=right_safety_clip,
        sw_version="test",
        event_time_ms=event_time_ms,
        state_machine=make_state_machine(session_id, state, since_session_start_ms),
        system_state="ONLINE",
        event=event,
    )
