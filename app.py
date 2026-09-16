from flask import Flask, jsonify, make_response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from dotenv import load_dotenv
import os
import sqlite3
import requests
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)
CORS(app, origins=["https://discord.wordbomb.io"])

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
WORDBOMB_API_TOKEN = os.environ.get("WORDBOMB_API_TOKEN_PROFILE")

limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})

try:
    mongo_client = MongoClient(MONGO_URI)
    mongo_db = mongo_client.questions
    approved_questions_collection = mongo_db.approved
    rejected_questions_collection = mongo_db.rejected
    print("[INFO] Flask app successfully connected to MongoDB.")
except Exception as e:
    mongo_client = None
    print(f"[ERROR] Flask app failed to connect to MongoDB: {e}")


def get_discord_user(user_id):
    if not DISCORD_BOT_TOKEN: return None
    url = f"https://discord.com/api/v9/users/{user_id}"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Discord user: {e}")
        return None


@app.route("/")
def home():
    return jsonify({"message": "App is deployed and running!"})


@app.route("/api/user/<int:user_id>")
@limiter.limit("10 per minute")
@cache.memoize(timeout=60)
def api_user(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        stats = {}
        cursor.execute("SELECT seconds FROM voice_time WHERE user_id = ?", (user_id,));
        row = cursor.fetchone();
        stats["voice_time_seconds"] = row[0] if row else 0;
        stats["voice_time_hours"] = round(row[0] / 3600, 2) if row else 0
        cursor.execute("SELECT count FROM messages WHERE user_id = ?", (user_id,));
        row = cursor.fetchone();
        stats["messages"] = row[0] if row else 0
        cursor.execute("SELECT count FROM bug_points WHERE user_id = ?", (user_id,));
        row = cursor.fetchone();
        stats["bug_points"] = row[0] if row else 0
        cursor.execute("SELECT count FROM idea_points WHERE user_id = ?", (user_id,));
        row = cursor.fetchone();
        stats["idea_points"] = row[0] if row else 0
        cursor.execute("SELECT count FROM candies WHERE user_id = ?", (user_id,));
        row = cursor.fetchone();
        stats["candies"] = row[0] if row else 0
        conn.close()

        if approved_questions_collection is not None and rejected_questions_collection is not None:
            approved_count = approved_questions_collection.count_documents({"u": str(user_id)})
            rejected_count = rejected_questions_collection.count_documents({"u": str(user_id)})
            stats["total_questions_suggested"] = approved_count + rejected_count
        else:
            stats["total_questions_suggested"] = "N/A"

        if not any(v for v in stats.values() if v != "N/A" and v != 0):
            return jsonify({"error": f"No data found for user {user_id}."}), 404

        discord_user = get_discord_user(user_id)
        username = discord_user.get('username') if discord_user else "Unknown"

        response_data = {"user_id": user_id, "discord_username": username, "stats": stats}
        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/<int:user_id>/questions-details")
@limiter.limit("20 per minute")
@cache.memoize(timeout=120)
def user_questions_details(user_id):
    if approved_questions_collection is None or rejected_questions_collection is None:
        return jsonify({"error": "Database connection not available."}), 503

    try:
        user_id_str = str(user_id)
        approved_count = approved_questions_collection.count_documents({"u": user_id_str})
        rejected_count = rejected_questions_collection.count_documents({"u": user_id_str})
        total_suggestions = approved_count + rejected_count

        if total_suggestions == 0:
            acceptance_rate = 0
        else:
            acceptance_rate = round((approved_count / total_suggestions) * 100, 2)

        response_data = {
            "user_id": user_id,
            "total_suggestions": total_suggestions,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "acceptance_rate_percent": acceptance_rate,
        }

        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/<int:user_id>/messages-details")
@limiter.limit("10 per minute")
@cache.memoize(timeout=60)
def user_message_details(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT rank FROM (SELECT user_id, RANK() OVER (ORDER BY count DESC) as rank FROM messages) WHERE user_id = ?",
            (user_id,))
        rank_data = cursor.fetchone()
        leaderboard_position = rank_data['rank'] if rank_data else None
        history_query = """
            WITH AllUserCumulativeTotals AS (
                SELECT user_id, week, SUM(count) OVER (PARTITION BY user_id ORDER BY week ASC) as cumulative_messages
                FROM message_history WHERE LENGTH(week) = 7
            ), RankedWeekly AS (
                SELECT user_id, week, RANK() OVER (PARTITION BY week ORDER BY cumulative_messages DESC) as rank_at_end_of_week
                FROM AllUserCumulativeTotals
            )
            SELECT mh.week, mh.count, rw.rank_at_end_of_week as rank
            FROM message_history mh
            JOIN RankedWeekly rw ON mh.user_id = rw.user_id AND mh.week = rw.week
            WHERE mh.user_id = ? ORDER BY mh.week ASC;
        """
        cursor.execute(history_query, (user_id,))
        history_rows = cursor.fetchall()
        conn.close()
        message_data = [{"week": row["week"], "count": row["count"], "rank": row["rank"]} for row in history_rows]
        return jsonify(
            {"user_id": user_id, "leaderboard_position": leaderboard_position, "messages_per_week": message_data})
    except Exception as e:
        print(f"Error in user_message_details for user {user_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/user/<int:user_id>/voice-time-details")
@limiter.limit("20 per minute")
@cache.memoize(timeout=180)
def user_voice_time_details(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        daily_activity_query = """
            SELECT
                SUM(duration_seconds) as total_seconds,
                CASE strftime('%w', start_timestamp)
                    WHEN '0' THEN 'Sunday'
                    WHEN '1' THEN 'Monday'
                    WHEN '2' THEN 'Tuesday'
                    WHEN '3' THEN 'Wednesday'
                    WHEN '4' THEN 'Thursday'
                    WHEN '5' THEN 'Friday'
                    WHEN '6' THEN 'Saturday'
                END as day
            FROM
                voice_sessions
            WHERE
                user_id = ?
            GROUP BY
                day
            ORDER BY
                total_seconds DESC;
        """
        cursor.execute(daily_activity_query, (user_id,))
        daily_activity_rows = cursor.fetchall()
        daily_activity_summary = [dict(row) for row in daily_activity_rows]

        longest_session_query = """
            SELECT
                duration_seconds,
                strftime('%Y-%m-%d', start_timestamp) as date
            FROM
                voice_sessions
            WHERE
                user_id = ?
            ORDER BY
                duration_seconds DESC
            LIMIT 1;
        """
        cursor.execute(longest_session_query, (user_id,))
        longest_session_row = cursor.fetchone()

        longest_session_data = {
            "duration_seconds": longest_session_row['duration_seconds'] if longest_session_row else 0,
            "date": longest_session_row['date'] if longest_session_row else None
        }

        conn.close()

        response_data = {
            "user_id": user_id,
            "daily_activity_summary": daily_activity_summary,
            "longest_session": longest_session_data
        }

        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response

    except Exception as e:
        print(f"Error in user_voice_time_details for user {user_id}: {e}")
        return jsonify({"error": "An internal error occurred or no voice data found."}), 500


@app.route("/api/user/<int:user_id>/bug-points-details")
@limiter.limit("20 per minute")
@cache.memoize(timeout=120)
def user_bug_points_details(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        total_points_query = "SELECT count FROM bug_points WHERE user_id = ?;"
        cursor.execute(total_points_query, (user_id,))
        total_points_data = cursor.fetchone()
        total_bug_points = total_points_data['count'] if total_points_data else 0

        heatmap_query = "SELECT strftime('%Y-%m-%d', report_timestamp) as date FROM bug_reports WHERE user_id = ?;"
        cursor.execute(heatmap_query, (user_id,))
        all_bug_dates = [row['date'] for row in cursor.fetchall()]

        recent_bugs_query = "SELECT description, strftime('%Y-%m-%d', report_timestamp) as date FROM bug_reports WHERE user_id = ? ORDER BY report_timestamp DESC LIMIT 5;"
        cursor.execute(recent_bugs_query, (user_id,))
        recent_bugs = [dict(row) for row in cursor.fetchall()]

        conn.close()

        first_bug_date = min(all_bug_dates) if all_bug_dates else None

        response_data = {
            "user_id": user_id,
            "total_bug_points": total_bug_points,
            "first_bug_date": first_bug_date,
            "contribution_dates": all_bug_dates,
            "recent_bugs": recent_bugs
        }

        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response

    except Exception as e:
        print(f"Error in user_bug_points_details for user {user_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/user/<int:user_id>/idea-points-details")
@limiter.limit("20 per minute")
@cache.memoize(timeout=120)
def user_idea_points_details(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        total_points_query = "SELECT count FROM idea_points WHERE user_id = ?;"
        cursor.execute(total_points_query, (user_id,))
        total_points_data = cursor.fetchone()
        total_idea_points = total_points_data['count'] if total_points_data else 0

        cursor.execute(
            "SELECT MIN(strftime('%Y-%m-%d', submission_timestamp)) as first_idea_date FROM idea_submissions WHERE user_id = ?;",
            (user_id,))
        first_idea_data = cursor.fetchone()
        first_idea_date = first_idea_data['first_idea_date'] if first_idea_data else None

        heatmap_query = "SELECT strftime('%Y-%m-%d', submission_timestamp) as date FROM idea_submissions WHERE user_id = ?;"
        cursor.execute(heatmap_query, (user_id,))
        all_idea_dates = [row['date'] for row in cursor.fetchall()]

        recent_ideas_query = "SELECT description, strftime('%Y-%m-%d', submission_timestamp) as date FROM idea_submissions WHERE user_id = ? ORDER BY submission_timestamp DESC LIMIT 5;"
        cursor.execute(recent_ideas_query, (user_id,))
        recent_ideas = [dict(row) for row in cursor.fetchall()]

        conn.close()

        response_data = {
            "user_id": user_id,
            "total_idea_points": total_idea_points,
            "first_idea_date": first_idea_date,
            "contribution_dates": all_idea_dates,
            "recent_ideas": recent_ideas
        }

        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response

    except Exception as e:
        print(f"Error in user_idea_points_details for user {user_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/user/<int:user_id>/candies-details")
@limiter.limit("20 per minute")
@cache.memoize(timeout=120)
def user_candies_details(user_id):
    try:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "server_data.db")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT count FROM candies WHERE user_id = ?;", (user_id,))
        candy_data = cursor.fetchone()

        candy_count = candy_data['count'] if candy_data else 0

        conn.close()

        response_data = {
            "user_id": user_id,
            "candy_count": candy_count
        }

        response = make_response(jsonify(response_data))
        response.headers["Cache-Control"] = "public, max-age=120, immutable"
        return response

    except Exception as e:
        print(f"Error in user_candies_details for user {user_id}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/user/<int:user_id>/wordbomb-profile")
@limiter.limit("20 per minute")
@cache.memoize(timeout=300)
def wordbomb_profile(user_id):
    if not WORDBOMB_API_TOKEN:
        return jsonify({"error": "Word Bomb API token is not configured."}), 500

    api_url = f"http://api.wordbomb.io/api/profile?u={user_id}"
    headers = {
        "Authorization": f"Bearer {WORDBOMB_API_TOKEN}"
    }

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        raw_data = response.json()

        profile_data = {
            "id": raw_data.get("id"),
            "displayName": raw_data.get("displayName", "N/A"),
            "avatarUrl": f"https://cdn.discordapp.com/avatars/{raw_data.get('id')}/{raw_data.get('avatar')}?size=256",
            "score": raw_data.get("score", 0),
            "wins": raw_data.get("win", 0),
            "wordCount": raw_data.get("wordCount", 0),
            "playTime": raw_data.get("playTime", 0),
            "longestWord": raw_data.get("longestWord", "N/A")
        }

        return jsonify(profile_data)

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({"error": "Word Bomb profile not found for this user."}), 404
        return jsonify({"error": f"Failed to fetch Word Bomb profile: {e.response.status_code}"}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error contacting Word Bomb API: {e}"}), 503

@app.errorhandler(429)
def ratelimit_handler(e):
    return make_response(jsonify({"error": "Rate limit exceeded", "message": str(e.description)}), 429)

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN: print("Warning: DISCORD_BOT_TOKEN environment variable is not set.")
    if not MONGO_URI: print("Warning: MONGO_URI environment variable is not set.")
    app.run(host="0.0.0.0", port=5000)
