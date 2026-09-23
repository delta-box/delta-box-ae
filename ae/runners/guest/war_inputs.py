"""Classify historical replay inputs before any measured filesystem writes.

The archived writer applies every diff to the recorded base file. Preserve that
protocol and report known out-of-bounds diffs as missing observations, not zeros.
"""
def prepare_actions(info, lower, apply_diff):
    eligible, excluded, mismatches = [], [], []
    for edit in info['edits']:
        original = (lower/edit['file_path']).read_bytes()
        headers = [line[4:].split('\t',1)[0] for line in edit['diff'].splitlines()
                   if line.startswith('--- ')]
        if headers and headers[0].removeprefix('a/') != edit['file_path']:
            mismatches.append(dict(edit_idx=edit['edit_idx'],file_path=edit['file_path'],diff_paths=headers))
        try:
            apply_diff(original,edit['diff'])
        except IndexError:
            excluded.append(dict(edit_idx=edit['edit_idx'],transition_id=edit['transition_id'],
                file_path=edit['file_path'],diff_bytes=edit['diff_bytes'],diff_paths=headers,
                applied_ok=False,measurement_status='excluded-input',
                exclusion_reason='historical-diff-out-of-bounds',
                file_size_bytes=len(original),copyup_bytes=None,phys_bytes=None))
        else:
            eligible.append(edit)
    return dict(info,edits=eligible,n_edits=len(eligible)), excluded, mismatches
