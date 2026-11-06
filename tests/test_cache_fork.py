"""Fork real Transformers caches without sharing mutable prefix or sibling state."""
import copy
from types import SimpleNamespace

import pytest

from qev.engine import DecisionEngine


@pytest.mark.parametrize('rows', [1, 2, 7])
def test_fork_isolates_attention_recurrent_and_metadata(rows):
    torch = pytest.importorskip('torch')
    from transformers.cache_utils import DynamicCache, DynamicLayer, DynamicSlidingWindowLayer, LinearAttentionLayer

    with torch.inference_mode():
        cache = DynamicCache()
        full, sliding, linear = DynamicLayer(), DynamicSlidingWindowLayer(sliding_window=3), LinearAttentionLayer(number_of_states=2)
        for layer in (full, sliding):
            layer.update(torch.arange(20.).reshape(1, 2, 5, 2), torch.ones(1, 2, 5, 2))
        for index in (0, 1):
            linear.update_conv_state(torch.arange(12.).reshape(1, 3, 4) + index, state_idx=index)
            linear.update_recurrent_state(torch.ones(1, 2, 3, 3) + index, state_idx=index)
        cache.layers = [full, sliding, linear, DynamicLayer(), LinearAttentionLayer()]
        before = copy.deepcopy(cache)
        forked = DecisionEngine._fork(None, cache, rows)
        sibling = DecisionEngine._fork(None, cache, rows)
        assert type(forked) is DynamicCache
        assert forked.layers[1].cumulative_length == 5
        assert forked.layers[1].sliding_window == 3
        assert forked.layers[2].has_previous_state == {0: True, 1: True}
        assert forked.layers[3].keys is None
        assert forked.layers[4].conv_states == {0: None}
        for original, expected, actual, other in zip(cache.layers[:3], before.layers[:3], forked.layers[:3], sibling.layers[:3]):
            fields = ('conv_states', 'recurrent_states') if isinstance(original, LinearAttentionLayer) else ('keys', 'values')
            for field in fields:
                source, reference, target, untouched = [getattr(layer, field) for layer in (original, expected, actual, other)]
                if isinstance(source, dict):
                    assert target is not source
                    pairs = [(source[k], reference[k], target[k], untouched[k]) for k in source]
                else:
                    pairs = [(source, reference, target, untouched)]
                for base, saved, expanded, independent in pairs:
                    torch.testing.assert_close(expanded, saved.repeat_interleave(rows, dim=0))
                    expanded[0].add_(100)
                    torch.testing.assert_close(base, saved)
                    torch.testing.assert_close(independent, saved.repeat_interleave(rows, dim=0))
                    if rows > 1:
                        torch.testing.assert_close(expanded[1], saved[0])
        forked.layers[2].has_previous_state[0] = False
        assert cache.layers[2].has_previous_state[0]


def test_unknown_cache_uses_its_own_repeat_method_on_a_copy():
    torch = pytest.importorskip('torch')

    class CustomLayer:
        def __init__(self):
            self.state = torch.ones(1, 3)
            self.repeats = []

        def batch_repeat_interleave(self, rows):
            self.state.add_(1)
            self.state = self.state.repeat_interleave(rows, dim=0)
            self.repeats.append(rows)

    cache = SimpleNamespace(layers=[CustomLayer()])
    forked = DecisionEngine._fork(None, cache, 3)
    assert cache.layers[0].repeats == []
    assert cache.layers[0].state.tolist() == [[1, 1, 1]]
    assert forked.layers[0].repeats == [3]
    assert forked.layers[0].state.tolist() == [[2, 2, 2]] * 3
