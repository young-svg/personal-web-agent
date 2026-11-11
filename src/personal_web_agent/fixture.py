"""P1 fixture business checks, separate from the shared browser/model loop."""
import httpx


async def read_state(origin):
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        response = await client.get(origin + '/api/state')
        response.raise_for_status()
        return response.json()


def validate_initial(state, human_handoff):
    if human_handoff and (state.get('fixture_version') != 'P1-C1' or state.get('approval_status') != 'LOCKED'):
        raise ValueError('Handoff requires the protected P1-C1 fixture in LOCKED state')
    if state.get('submit_count') != 0 or state.get('upload_count') != 0 or state.get('final_status') != 'NOT_SUBMITTED':
        raise ValueError('Fixture must be empty before a run; records will not be reset by the Runner')


async def observe(page, origin):
    from personal_web_agent.approval import read_form
    return await read_form(page), await read_state(origin)


def verify(inputs, page, state, history):
    from personal_web_agent.task_inputs import verify_preparation
    return verify_preparation(inputs, page, state, history.is_successful(), history.final_result())


def known_no_effects(state):
    return (state['submit_attempt_count'] == 0 and state['successful_submit_count'] == 0
            and state['approval_status'] in {'LOCKED', 'CANCELLED'})
