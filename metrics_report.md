# LLM Router Provider Metrics Report

**Generated:** 2026-08-10 13:55:52 UTC
**Period:** Last 7 days
**Database:** /home/hyperion/Code/projects/llm-router/metrics.db

## Summary

| Provider | Available | Calls Today | Calls Remaining | Tokens Today | Tokens Remaining | Rate Limit Remaining | Rate Limit Reset | p50 Latency | p99 Latency |
|----------|-----------|-------------|-----------------|--------------|------------------|---------------------|------------------|-------------|-------------|
| cerebras | ❓ | 146 | 4854 | 0 | 500000 | N/A | N/A | 261.5ms | 653.9ms |
| google_ai | ❓ | 146 | 854 | 0 | 100000 | N/A | N/A | 330.9ms | 827.4ms |
| groq | ❓ | 146 | 9854 | 0 | 1000000 | N/A | N/A | 196.8ms | 492.0ms |
| huggingface | ❓ | 154 | 9846 | 4 | 999996 | N/A | N/A | 380.4ms | 950.9ms |
| local | ❓ | 160 | N/A | 27 | N/A | N/A | N/A | 125.3ms | 313.1ms |
| nvidia | ❓ | 146 | 4854 | 0 | 500000 | N/A | N/A | 232.3ms | 580.8ms |

## Provider Details

### cerebras

#### Current Status

- **Calls Today:** 146 (0 failed)
- **Tokens Today:** 0 (0 prompt + 0 completion)
- **Latency (p50):** 261.5ms
- **Latency (p99):** 653.9ms

#### Quota Configuration

- **Daily Request Limit:** 5000
- **Daily Token Limit:** 500000
- **Reset Hour (UTC):** 0:00

#### Quota Remaining

- **Requests:** 4854 / 5000
- **Tokens:** 500000 / 500000

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 146 | 0 | 0 | 0 | 0 | 261.5ms | 653.9ms |
| 2026-08-09 | 29 | 0 | 0 | 0 | 0 | 260.2ms | 650.5ms |

### google_ai

#### Current Status

- **Calls Today:** 146 (0 failed)
- **Tokens Today:** 0 (0 prompt + 0 completion)
- **Latency (p50):** 330.9ms
- **Latency (p99):** 827.4ms

#### Quota Configuration

- **Daily Request Limit:** 1000
- **Daily Token Limit:** 100000
- **Reset Hour (UTC):** 0:00

#### Quota Remaining

- **Requests:** 854 / 1000
- **Tokens:** 100000 / 100000

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 146 | 0 | 0 | 0 | 0 | 330.9ms | 827.4ms |
| 2026-08-09 | 29 | 0 | 0 | 0 | 0 | 317.9ms | 794.7ms |

### groq

#### Current Status

- **Calls Today:** 146 (0 failed)
- **Tokens Today:** 0 (0 prompt + 0 completion)
- **Latency (p50):** 196.8ms
- **Latency (p99):** 492.0ms

#### Quota Configuration

- **Daily Request Limit:** 10000
- **Daily Token Limit:** 1000000
- **Reset Hour (UTC):** 0:00

#### Quota Remaining

- **Requests:** 9854 / 10000
- **Tokens:** 1000000 / 1000000

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 146 | 0 | 0 | 0 | 0 | 196.8ms | 492.0ms |
| 2026-08-09 | 29 | 0 | 0 | 0 | 0 | 211.7ms | 529.1ms |

### huggingface

#### Current Status

- **Calls Today:** 154 (4 failed)
- **Tokens Today:** 4 (2 prompt + 2 completion)
- **Latency (p50):** 380.4ms
- **Latency (p99):** 950.9ms

#### Quota Configuration

- **Daily Request Limit:** 10000
- **Daily Token Limit:** 1000000
- **Reset Hour (UTC):** 0:00

#### Quota Remaining

- **Requests:** 9846 / 10000
- **Tokens:** 999996 / 1000000

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 154 | 4 | 2 | 2 | 4 | 380.4ms | 950.9ms |
| 2026-08-09 | 38 | 4 | 36 | 391 | 427 | 375.7ms | 939.1ms |

### local

#### Current Status

- **Calls Today:** 160 (0 failed)
- **Tokens Today:** 27 (21 prompt + 6 completion)
- **Latency (p50):** 125.3ms
- **Latency (p99):** 313.1ms

#### Quota Configuration

- **No quota configured**

#### Quota Remaining

- **Requests:** Unlimited / Not configured
- **Tokens:** Unlimited / Not configured

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 160 | 0 | 21 | 6 | 27 | 125.3ms | 313.1ms |
| 2026-08-09 | 40 | 0 | 19 | 49 | 68 | 194.7ms | 486.7ms |

### nvidia

#### Current Status

- **Calls Today:** 146 (0 failed)
- **Tokens Today:** 0 (0 prompt + 0 completion)
- **Latency (p50):** 232.3ms
- **Latency (p99):** 580.8ms

#### Quota Configuration

- **Daily Request Limit:** 5000
- **Daily Token Limit:** 500000
- **Reset Hour (UTC):** 0:00

#### Quota Remaining

- **Requests:** 4854 / 5000
- **Tokens:** 500000 / 500000

#### Rate Limit (from provider headers)

- **No rate limit data available**

#### Last 2 Days History

| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |
|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|
| 2026-08-10 | 146 | 0 | 0 | 0 | 0 | 232.3ms | 580.8ms |
| 2026-08-09 | 31 | 2 | 0 | 0 | 0 | 7951.7ms | 19879.1ms |

---
*Report generated at 2026-08-10 13:55:52 UTC*