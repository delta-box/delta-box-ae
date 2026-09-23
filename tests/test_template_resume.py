"""A template can consume its queued command without re-entering the agent loop."""
import unittest
from unittest.mock import patch
from backends.deltabox.gsd import template_fork as tf


class ResumeTests(unittest.TestCase):
    def test_resumed_template_processes_next_command_and_returns_child(self):
        payload = b'probe'
        with patch.object(tf, '_handle_template_message',
                          side_effect=['parent_resumed', 'child']) as handler:
            self.assertEqual(tf.handle_template_message(1, '/out', payload), 'child')
        self.assertEqual(handler.call_count, 2)
        for args in handler.call_args_list:
            self.assertEqual(args.args, (1, '/out', payload))

    def test_active_roles_return_without_consuming_another_command(self):
        for role in ('child', 'parent_active', None):
            with self.subTest(role=role), patch.object(
                    tf, '_handle_template_message', return_value=role) as handler:
                self.assertEqual(tf.handle_template_message(1, '/out'), role)
                handler.assert_called_once()

    def test_unsolicited_continue_without_complete_command_returns_to_agent(self):
        with patch.object(tf, '_handle_template_message',
                          side_effect=['parent_resumed', None]) as handler:
            self.assertEqual(tf.handle_template_message(1, '/out'), 'parent_resumed')
        self.assertEqual(handler.call_count, 2)

    def test_many_restores_do_not_grow_python_stack(self):
        with patch.object(tf, '_handle_template_message',
                          side_effect=['parent_resumed'] * 2000 + ['parent_active']):
            self.assertEqual(tf.handle_template_message(1, '/out'), 'parent_active')


if __name__ == '__main__':
    unittest.main()
