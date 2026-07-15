from __future__ import annotations

import time

from context_pager.config import settings
from context_pager.deps import Dependencies


LAMBDA = 0.01  # decay rate
MIN_RELEVANCE = 0.1

DECAY_LUA = """
local key = KEYS[1]
local lambda = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local min_score = tonumber(ARGV[3])

local entries = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
for i = 1, #entries, 2 do
    local member = entries[i]
    local score = tonumber(entries[i + 1])
    local age_minutes = (now - score) / 60000
    local new_score = score * math.exp(-lambda * age_minutes)
    if new_score < min_score then
        redis.call('ZREM', key, member)
    else
        redis.call('ZADD', key, new_score, member)
    end
end
"""


async def decay_active_pages() -> None:
    """Run decay on all active page sets."""
    r = await Dependencies.redis()
    keys = await r.keys("pager:active:*")
    now_ms = time.time() * 1000

    for key in keys:
        await r.eval(DECAY_LUA, 1, key, LAMBDA, now_ms, MIN_RELEVANCE)