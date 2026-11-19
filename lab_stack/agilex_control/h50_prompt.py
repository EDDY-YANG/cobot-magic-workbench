"""Per-run language input only; never changes the approved control contract."""


def validate_prompt(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or '\x00' in value:
        raise ValueError('task必须为1至200字符的非空任务提示词，不能包含NUL')
    return value
