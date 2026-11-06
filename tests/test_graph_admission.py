"""Memory admission must happen before speculative decoder work, without a GPU."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from qev.mm_engine import DecoderGraphs, ForkGraphs, GraphMemoryLow


@pytest.mark.parametrize('path', ['decoder', 'prefill', 'branch'])
@pytest.mark.parametrize('lose_memory_after_warmup', [False, True])
def test_unavailable_capture_checks_memory_before_warmup_and_again_afterwards(monkeypatch, path, lose_memory_after_warmup):
    torch = pytest.importorskip('torch')
    calls, checks = [], []
    stream = SimpleNamespace(wait_stream=lambda other: None)
    monkeypatch.setattr(torch.cuda, 'Stream', lambda **kwargs: stream)
    monkeypatch.setattr(torch.cuda, 'current_stream', lambda: stream)
    monkeypatch.setattr(torch.cuda, 'stream', lambda side: nullcontext())

    def decoder(inputs_embeds=None, input_ids=None, **kwargs):
        calls.append('forward')
        hidden = inputs_embeds if inputs_embeds is not None else torch.zeros((*input_ids.shape, 8))
        return SimpleNamespace(last_hidden_state=hidden, past_key_values=hidden[:, -1].clone())

    def check(evict):
        checks.append('check')
        if not lose_memory_after_warmup or len(checks) == 2:
            raise GraphMemoryLow('unavailable memory reserve')

    monkeypatch.setattr('qev.mm_engine._make_room', check)
    embeds = torch.zeros((1, 4, 8))
    positions = torch.arange(4).view(1, 1, -1).expand(3, 1, -1)
    graphs = DecoderGraphs(decoder) if path == 'decoder' else ForkGraphs(decoder, lambda cache, rows: cache, 0)
    prefix = (embeds, positions, torch.tensor(0), embeds[:, -1], object())
    if path == 'branch':
        graphs.prefills[4] = prefix
    with pytest.raises(GraphMemoryLow, match='reserve'):
        if path == 'decoder':
            graphs._capture(4, embeds, positions)
        elif path == 'prefill':
            graphs._capture_prefill(4, embeds, positions)
        else:
            graphs._capture_branch((4, 2, 2), prefix[3], torch.long)
    assert len(checks) == (2 if lose_memory_after_warmup else 1)
    assert len(calls) == (2 if lose_memory_after_warmup else 0)
    if path == 'decoder':
        assert not graphs.graphs
    else:
        assert not graphs.branches
        assert list(graphs.prefills) == ([4] if path == 'branch' else [])
        if path == 'branch':
            assert graphs.prefills[4] is prefix


@pytest.mark.parametrize('forked', [False, True], ids=['decoder', 'fork'])
@pytest.mark.parametrize('reason', ['shape', 'memory'])
def test_graph_admission_skips_lazy_vision_work_when_it_will_fall_back(monkeypatch, forked, reason):
    from unittest.mock import Mock
    torch = pytest.importorskip('torch')
    embedding = Mock(side_effect=AssertionError('vision encoding must not run'))
    positions = torch.zeros((3, 1, 4), dtype=torch.long)
    graphs = (ForkGraphs(None, None, pad_id=0, step=4, max_len=2 if reason == 'shape' else 8)
              if forked else DecoderGraphs(None, sizes=(2,) if reason == 'shape' else (8,)))

    def no_room(evict):
        raise GraphMemoryLow('memory reserve')

    monkeypatch.setattr('qev.mm_engine._make_room', no_room)

    def run():
        return graphs(embedding, positions, [[1], [2]], 0) if forked else graphs(embedding, positions)

    if reason == 'shape':
        assert run() is None
    else:
        with pytest.raises(GraphMemoryLow, match='reserve'):
            run()
    embedding.assert_not_called()


def test_branch_admission_keeps_the_prefix_its_capture_will_read(monkeypatch):
    from unittest.mock import Mock
    torch = pytest.importorskip('torch')
    graphs = ForkGraphs(None, None, pad_id=0, step=4, max_len=16)
    current, other = object(), object()
    graphs.prefills.update({4: current, 8: other})
    graphs.branches.update({(4, 3, 16): object(), (8, 2, 16): object()})
    embedding = Mock(side_effect=AssertionError('admission must finish first'))

    def exhaust_available_evictions(evict):
        while evict():
            assert graphs.prefills.get(4) is current
        raise GraphMemoryLow('insufficient reserve after eviction')

    monkeypatch.setattr('qev.mm_engine._make_room', exhaust_available_evictions)
    with pytest.raises(GraphMemoryLow):
        graphs(embedding, torch.zeros((3, 1, 4), dtype=torch.long), [[1], [2]], 0)
    assert graphs.prefills == {4: current}
    assert not graphs.branches
    embedding.assert_not_called()


@pytest.mark.parametrize('forked', [False, True], ids=['decoder', 'fork'])
def test_cached_replay_does_not_require_capture_memory(monkeypatch, forked):
    from unittest.mock import Mock
    torch = pytest.importorskip('torch')
    embeds = torch.zeros((1, 4, 8))
    positions = torch.arange(4).view(1, 1, -1).expand(3, 1, -1)
    source = Mock(return_value=torch.ones_like(embeds))
    no_room = Mock(side_effect=GraphMemoryLow('no room for a new capture'))
    monkeypatch.setattr('qev.mm_engine._make_room', no_room)
    graph = SimpleNamespace(replay=Mock())
    if forked:
        graphs = ForkGraphs(None, None, pad_id=0, step=4, width_step=2)
        graphs.prefills[4] = (embeds, positions.clone(), torch.tensor(0), object(), graph)
        branch = SimpleNamespace(replay=Mock())
        out = torch.arange(32).reshape(2, 2, 8)
        graphs.branches[(4, 2, 2)] = (torch.zeros((2, 2), dtype=torch.long), torch.tensor(0), torch.tensor(0), out, branch)
        result = graphs(source, positions, [[1], [2, 3]], 0)
        torch.testing.assert_close(result, torch.stack([out[0, 0], out[1, 1]]))
        branch.replay.assert_called_once()
    else:
        graphs = DecoderGraphs(None, sizes=(4,))
        out = torch.arange(32).reshape(1, 4, 8)
        graphs.graphs[4] = (embeds, positions.clone(), out, graph)
        torch.testing.assert_close(graphs(source, positions), out[:, -1])
    no_room.assert_not_called()
    source.assert_called_once()
    graph.replay.assert_called_once()
    torch.testing.assert_close(embeds, torch.ones_like(embeds))
