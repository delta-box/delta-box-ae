"""Pure recovery accepts original response thoughts and rejects contract damage."""
import copy
import importlib.util
from pathlib import Path
import unittest

SOURCE=Path(__file__).resolve().parents[2]/'ae/vendor/finalbench/e2b_finalbench/e2b_paper_action.py'
spec=importlib.util.spec_from_file_location('pure_paper_action_under_test',SOURCE)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


def node(thought=None):
    return {'action_steps':[{'action':{'action_args_class':'pkg.FindClassArgs',
        'thoughts':thought,'class_name':'Storage','file_pattern':None}}],
        'completions':{'build_action':{'response':{'choices':[{'message':{
            'content':'Thought: original reasoning\nAction: FindClass\n{"class_name":"Storage"}'}}]}}}}


class RecoveryTests(unittest.TestCase):
    def recover(self,value,index=0,klass='pkg.FindClassArgs',name='FindClass'):
        return helper.recorded_action_payload(value,index,klass,name)

    def test_null_empty_and_matching_nonempty_recover_without_mutation(self):
        for thought in [None,'','original reasoning']:
            with self.subTest(thought=thought):
                value=node(thought);before=copy.deepcopy(value)
                recovered=self.recover(value)
                self.assertEqual(value,before)
                self.assertEqual(recovered,dict(before['action_steps'][0]['action'],
                                                thoughts='original reasoning'))

    def test_nonempty_original_conflict_rejected(self):
        with self.assertRaisesRegex(ValueError,'conflicts'):
            self.recover(node('other retained reasoning'))

    def test_wrong_class_or_name_rejected(self):
        for kwargs in [{'klass':'pkg.OtherArgs'},{'name':'Other'}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                self.recover(node(),**kwargs)

    def test_choice_count_and_nontext_response_rejected(self):
        for change in [lambda c:c.clear(),lambda c:c.append(copy.deepcopy(c[0])),
                       lambda c:c[0]['message'].update(content=None)]:
            value=node();change(value['completions']['build_action']['response']['choices'])
            with self.assertRaises(ValueError):self.recover(value)

    def test_duplicate_missing_reversed_or_bodyless_blocks_rejected(self):
        texts=['Thought: one\nThought: two\nAction: FindClass\n{}',
               'Thought: one\nAction: FindClass\n{}\nAction: FindClass\n{}',
               'Action: FindClass\n{}', 'Thought: one',
               'Action: FindClass\n{}\nThought: one',
               'Thought: one\nAction: FindClass']
        for text in texts:
            value=node();value['completions']['build_action']['response']['choices'][0]['message']['content']=text
            with self.subTest(text=text),self.assertRaises(ValueError):self.recover(value)

    def test_multiple_recorded_actions_or_nonzero_index_rejected(self):
        value=node();value['action_steps'].append(copy.deepcopy(value['action_steps'][0]))
        with self.assertRaises(ValueError):self.recover(value)
        with self.assertRaises(ValueError):self.recover(node(),index=1)

    def test_nontext_recorded_thought_rejected(self):
        for value in [False,7,[],{}]:
            with self.subTest(value=value),self.assertRaises(ValueError):self.recover(node(value))


if __name__=='__main__':unittest.main()
