"""Minimal, spec-compliant MCP server wrapping python-jobspy.

Third-party jobspy MCP forks (e.g. borgius/jobspy-mcp-server) implement their
own non-standard HTTP routes (/mcp/connect, /mcp/request) instead of the real
MCP protocol, so a generic MCP client (like Amy's amy/connectors/mcp.py)
can't talk to them. This server uses the same official `mcp` library Amy's
client uses (FastMCP), so it speaks the protocol correctly by construction.

Run:
    python mcp_servers/jobspy_server.py

Then in Amy (Account -> MCP Sources -> Add source):
    Name:        Job Search
    Server URL:  http://localhost:8935/mcp
    Auth type:   none
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from jobspy import scrape_jobs

mcp = FastMCP("JobSpy", host="0.0.0.0", port=8935)


@mcp.tool()
def search_jobs(
    search_term: str,
    location: str = "",
    site_names: str = "indeed",
    results_wanted: int = 20,
    hours_old: int = 72,
    is_remote: bool = False,
    country_indeed: str = "USA",
) -> list[dict]:
    """Search jobs across job boards in one call.

    site_names: comma-separated, from indeed, linkedin, zip_recruiter,
    glassdoor, google, bayt, naukri (e.g. "indeed,linkedin").
    country_indeed: MUST match location's country (e.g. "India" for
    Bangalore, "USA" for San Francisco) when site_names includes indeed —
    a mismatch silently returns zero results instead of an error.
    Returns one dict per job posting (title, company, location, job_url,
    date_posted, job_type, is_remote, salary fields, description, ...).
    """
    import sys

    sites = [s.strip() for s in site_names.split(",") if s.strip()]
    records: list[dict] = []
    # One site per scrape_jobs call, each in its own try/except: LinkedIn
    # rate-limits aggressively and any single blocked/unsupported board must
    # degrade to fewer results, never to a failed search.
    for site in sites:
        try:
            kwargs = {}
            if site == "google":
                # jobspy's Google Jobs scraper searches google_search_term,
                # not search_term — without it the board returns nothing.
                kwargs["google_search_term"] = (
                    f"{search_term} jobs in {location}" if location
                    else f"{search_term} jobs")
            df = scrape_jobs(
                site_name=[site],
                search_term=search_term,
                location=location or None,
                results_wanted=results_wanted,
                hours_old=hours_old,
                is_remote=is_remote,
                country_indeed=country_indeed,
                **kwargs,
            )
            # NaN isn't valid JSON — scrape_jobs leaves missing fields as NaN.
            df = df.where(df.notnull(), None)
            records.extend(df.to_dict(orient="records"))
        except Exception as exc:
            print(f"jobspy: site {site!r} failed, continuing: {exc}",
                  file=sys.stderr)
    return records


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
