"""Selectors for the heavy deltabox search loop."""
from __future__ import annotations

from moatless.node import Node


class DeepPatchFirstSelector:
    """Prefer continuing deep trajectories over expanding shallow siblings.

    The upstream SimpleSelector returns the first expandable node from a DFS
    pre-order list. With max_expansions=2 that repeatedly revisits shallow
    parents and burns budget on near-duplicate exploration. Agent actions are
    expensive and stateful, so we first need one deep trajectory that reaches
    an actual patch; rollback can then explore alternatives.
    """

    def select(self, expandable_nodes: list[Node]) -> Node | None:
        if not expandable_nodes:
            return None

        def rank(n: Node):
            # Prefer deeper nodes, then leaves, then newer node IDs.
            return (n.get_depth(), n.is_leaf(), n.node_id)

        return max(expandable_nodes, key=rank)
