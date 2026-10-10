"""VIDUR's Wire as a feed (Group 78): the news, the venue weather, ESPN's scores, team sheets, injuries and officials,
turned into the fortress's evidence (``app/services/twin/intel.py``) and The Wire's own record.

    fixtures      the fixtures the live board tracks (in play and up to WIRE_HORIZON_HOURS ahead)
    venues        where a fixture is played: indoor sport, an entered venue, the seed, a tournament, ESPN + geocoder
    weather       Open-Meteo for the match window -> wire_weather_snapshots + the weather section
    espn_sync     scoreboards and summaries -> scores with the clock, venues, team sheets (lineups section),
                  injuries (wire_injury_roster_reports, the injuries section once rated), officiating records
    lineups       absences, the operator's ratings, the lineup delta and the injuries section
    referees      assignments, records, profiles and the referee section
    news          RSS + newsapi.org -> sentiment, impact, credibility, mentions -> catalysts and alerts
    live          the /ws/the-wire channel

Every scan runs from the beat (``app/workers/the_wire_tasks.py``) and on demand (``POST /the-wire/scan``).
Developed for Amit Ashok Kumar Patnaik.
"""
