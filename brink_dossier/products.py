"""Commercial product profiles for The Brink World.

These profiles control report positioning and evidence emphasis without changing
the underlying scientific telemetry. They deliberately avoid claiming
certification, insurance pricing, regulatory assurance or engineering opinion.
"""

PRODUCTS = {
    "location_dossier": {
        "title": "Location Threat Dossier",
        "tier": "Decision-Support Intelligence",
        "short_name": "Location Dossier",
        "recurring": False,
        "intended_use": "General location intelligence and operational context.",
        "limitation": "Not an engineering, insurance, legal or regulatory opinion.",
    },
    "facility_risk_passport": {
        "title": "Facility Risk Passport",
        "tier": "External Risk Monitoring",
        "short_name": "Risk Passport",
        "recurring": True,
        "intended_use": (
            "Recurring external-risk monitoring for a named facility, including "
            "current warnings, nearby hazard signals, weather context, access and evidence provenance."
        ),
        "limitation": (
            "Supports facility risk management and monitoring. It does not certify "
            "the facility, estimate insured loss or replace site-specific engineering assessment."
        ),
    },
    "physical_risk_evidence_pack": {
        "title": "Physical Risk Evidence Pack",
        "tier": "Audit-Ready Evidence Support",
        "short_name": "Risk Evidence Pack",
        "recurring": True,
        "intended_use": (
            "Structured physical-risk evidence for sustainability, enterprise-risk "
            "and climate-risk assessment workflows."
        ),
        "limitation": (
            "Evidence support only. It is not assurance, certification or a statement "
            "that any reporting framework has been fully complied with."
        ),
    },
    "pre_underwriting_site_intelligence": {
        "title": "Pre-Underwriting Site Intelligence",
        "tier": "Commercial Property Evidence",
        "short_name": "Pre-Underwriting Site Intelligence",
        "recurring": False,
        "intended_use": (
            "Sourced external-hazard context for brokers, risk surveyors, insureds "
            "and commercial property review."
        ),
        "limitation": (
            "Does not determine insurability, premium, probable maximum loss, policy "
            "terms or claims outcome."
        ),
    },
    "business_continuity_threat_register": {
        "title": "External Threat Register",
        "tier": "Business Continuity Support",
        "short_name": "External Threat Register",
        "recurring": True,
        "intended_use": (
            "A maintained external-threat register for business-continuity and "
            "operational-resilience workflows."
        ),
        "limitation": (
            "Supports a continuity-management process but does not constitute ISO "
            "certification or replace the organisation's own business-impact analysis."
        ),
    },
}


def get_product_profile(product_type):
    key = str(product_type or "location_dossier").strip().lower()
    return key, PRODUCTS.get(key, PRODUCTS["location_dossier"])
