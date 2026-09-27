"""Pure contract recovery for the frozen original E2B paper recordings.

This does not manufacture action requests or ignore thoughts. The full recorded
action supplies functional fields; the same node's retained ReAct completion
supplies its exact thought text. Original request bytes were not retained.
"""
from copy import deepcopy


def recorded_action_payload(node, index, action_class, action_name):
    if index != 0 or len(node.get('action_steps', [])) != 1:
        raise ValueError('Original ReAct completion must bind exactly one recorded action')
    recorded = deepcopy(node['action_steps'][index]['action'])
    if recorded.get('action_args_class') != action_class:
        raise ValueError('Recorded action class differs from the frozen contract')
    original_thought = recorded.get('thoughts')
    if original_thought is not None and not isinstance(original_thought, str):
        raise ValueError('Recorded thoughts must be text or null')
    choices = node['completions']['build_action']['response']['choices']
    if len(choices) != 1:
        raise ValueError('Recorded ReAct completion must have exactly one choice')
    text = choices[0]['message']['content']
    if not isinstance(text, str):
        raise ValueError('Recorded ReAct completion has no text response')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if (sum(line.startswith('Thought:') for line in lines) != 1
            or sum(line.startswith('Action:') for line in lines) != 1):
        raise ValueError('Recorded ReAct completion must have one Thought/Action block')
    thought_start, action_start = text.find('Thought:'), text.find('Action:')
    if thought_start < 0 or action_start <= thought_start:
        raise ValueError('Recorded ReAct Thought/Action order is invalid')
    thought = text[thought_start + 8:action_start].strip()
    action_parts = text[action_start + 7:].strip().split('\n', 1)
    if (len(action_parts) != 2 or not action_parts[1].strip()
            or action_parts[0].strip() != action_name):
        raise ValueError('Recorded completion action differs from the frozen contract')
    if original_thought and original_thought != thought:
        raise ValueError('Recorded completion conflicts with retained original thoughts')
    recorded['thoughts'] = thought
    return recorded
