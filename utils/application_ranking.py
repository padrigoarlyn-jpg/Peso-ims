"""
Shared application ranking utilities for PESO-IMS.

This module is the single ranking source used by both:
- PESO Admin job rankings
- Employer job shortlist

It does not change database records. It only calculates
temporary match/ranking attributes for application objects.
"""

from website.utils.job_matching import calculate_job_match


def apply_match_result_to_application(
    application,
    match_result,
):
    """
    Copy the explainable match result onto an Application
    instance for template/route compatibility.

    These are temporary Python attributes unless a matching
    column already exists in the database model.
    """

    application.match_percentage = (
        match_result["match_percentage"]
    )

    # Existing employer templates historically treated
    # match_score as the displayed score. Keep it as percentage.
    application.match_score = (
        match_result["match_percentage"]
    )

    application.earned_points = (
        match_result["earned_points"]
    )

    application.total_points = (
        match_result["total_points"]
    )

    # Raw criteria count stays separate from weighted points.
    application.total_requirements = match_result.get(
        "criteria_total",
        len(match_result["details"]),
    )
    application.matched_criteria_count = match_result.get(
        "criteria_matched",
        sum(1 for detail in match_result["details"] if detail["matched"]),
    )

    application.all_required_met = (
        match_result["all_required_met"]
    )

    application.required_total = (
        match_result["required_total"]
    )

    application.required_matched = (
        match_result["required_matched"]
    )

    application.preferred_total = (
        match_result["preferred_total"]
    )

    application.preferred_matched = (
        match_result["preferred_matched"]
    )

    application.normalized_education = (
        match_result["education_label"]
    )

    application.total_experience_months = (
        match_result[
            "total_experience_months"
        ]
    )

    application.relevant_experience_months = (
        match_result[
            "relevant_experience_months"
        ]
    )
    application.experience_display = match_result.get(
        "total_experience_display",
        "No work experience recorded",
    )
    application.relevant_experience_display = match_result.get(
        "relevant_experience_display",
        "No relevant work experience recorded",
    )
    application.most_recent_work = match_result.get(
        "most_recent_work",
        "No work experience recorded",
    )

    application.match_details = (
        match_result["details"]
    )

    application.matched_requirements = [
        detail["label"]
        for detail in match_result["details"]
        if detail["matched"]
    ]

    application.unmatched_requirements = [
        detail["label"]
        for detail in match_result["details"]
        if not detail["matched"]
    ]

    application.ranking_status = (
        match_result["ranking_status"]
    )

    application.ranking_reason = (
        match_result["ranking_reason"]
    )

    application.education_match = None
    application.experience_match = None

    for detail in application.match_details:
        if detail["type"] == "education":
            application.education_match = (
                detail["matched"]
            )

        elif detail["type"] == "experience":
            application.experience_match = (
                detail["matched"]
            )

    return application


def initialize_unmatched_application(
    application,
    reason="Applicant profile is unavailable.",
):
    """
    Apply safe defaults when an Application has no usable
    JobSeeker profile.
    """

    application.match_percentage = 0.0
    application.match_score = 0.0

    application.earned_points = 0
    application.total_points = 0
    application.total_requirements = 0

    application.all_required_met = False

    application.required_total = 0
    application.required_matched = 0

    application.preferred_total = 0
    application.preferred_matched = 0

    application.normalized_education = None

    application.total_experience_months = 0
    application.relevant_experience_months = 0
    application.experience_display = "No work experience recorded"
    application.relevant_experience_display = "No relevant work experience recorded"
    application.most_recent_work = "No work experience recorded"
    application.matched_criteria_count = 0

    application.match_details = []
    application.matched_requirements = []
    application.unmatched_requirements = []

    application.education_match = None
    application.experience_match = None

    application.ranking_status = "Not Ranked"
    application.ranking_reason = reason

    application.rank = None

    return application


def application_ranking_sort_key(
    application,
):
    """
    Shared deterministic ranking order.

    Priority:
    1. Higher match percentage
    2. All required criteria met
    3. More required criteria matched
    4. More preferred criteria matched
    5. Lower application ID for stable ties
    """

    return (
        -float(
            getattr(
                application,
                "match_percentage",
                0
            )
            or 0
        ),
        -int(
            bool(
                getattr(
                    application,
                    "all_required_met",
                    False
                )
            )
        ),
        -int(
            getattr(
                application,
                "required_matched",
                0
            )
            or 0
        ),
        -int(
            getattr(
                application,
                "preferred_matched",
                0
            )
            or 0
        ),
        int(
            getattr(
                application,
                "application_id",
                0
            )
            or 0
        ),
    )


def rank_job_applications(
    job,
    applications,
):
    """
    Calculate structured match results, assign ranks, and split
    applications into Ranked and Not Ranked lists.

    Ranked:
        calculate_job_match() returns
        ranking_status == "Ranked"

    Not Ranked:
        no JobSeeker profile,
        no structured job requirements,
        or no structured criteria matched.

    Returns:
        {
            "all": [...],
            "ranked": [...],
            "unranked": [...],
        }
    """

    evaluated = []

    for application in applications:
        application.rank = None

        seeker = getattr(
            application,
            "job_seeker",
            None
        )

        if seeker is None:
            initialize_unmatched_application(
                application
            )

            evaluated.append(
                application
            )

            continue

        match_result = calculate_job_match(
            job,
            seeker
        )

        apply_match_result_to_application(
            application,
            match_result
        )

        evaluated.append(
            application
        )

    evaluated.sort(
        key=application_ranking_sort_key
    )

    ranked = [
        application
        for application in evaluated
        if (
            getattr(
                application,
                "ranking_status",
                "Not Ranked"
            )
            == "Ranked"
        )
    ]

    unranked = [
        application
        for application in evaluated
        if (
            getattr(
                application,
                "ranking_status",
                "Not Ranked"
            )
            != "Ranked"
        )
    ]

    rank_counter = 1

    for application in ranked:
        application.rank = rank_counter
        rank_counter += 1

    for application in unranked:
        application.rank = None

    return {
        "all": evaluated,
        "ranked": ranked,
        "unranked": unranked,
    }
