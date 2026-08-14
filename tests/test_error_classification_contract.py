from llm_router.providers.base import (
    ERROR_BILLING_OR_QUOTA,
    ERROR_PROVIDER_UNAVAILABLE,
    QuotaExceededError,
    ProviderUnavailable,
)


def test_quota_and_provider_outage_classes_remain_distinct() -> None:
    quota = QuotaExceededError("daily free quota exhausted", provider="demo")
    outage = ProviderUnavailable("upstream unavailable", status_code=503)

    assert quota.error_class == ERROR_BILLING_OR_QUOTA
    assert outage.error_class == ERROR_PROVIDER_UNAVAILABLE
