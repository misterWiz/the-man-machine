import collections
import datetime
import http
import itertools
import json
import logging
import os
import random
import re
import sys
import uuid
import textwrap
import flask
import flask_migrate
import flask_sqlalchemy
from slack import WebClient
from slack.errors import SlackApiError
from slackeventsapi import SlackEventAdapter
import sqlalchemy.sql


# Init fundamental stuff
app = flask.Flask(__name__)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
slack_events_adapter = SlackEventAdapter(
    os.environ.get("SLACK_SIGNING_SECRET"), "/slack/events", app
)
client = WebClient(os.environ.get("SLACK_OATH_TOKEN"))

# Database Stuff

db = flask_sqlalchemy.SQLAlchemy(app)
migrate = flask_migrate.Migrate(app, db)


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.String())
    uid = db.Column(db.String(), default=uuid.uuid4)
    channel_name = db.Column(db.String())
    channel_topic = db.Column(db.String())
    channel_id = db.Column(db.String())
    full_explanation = db.Column(db.String())
    submission_time = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    active = db.Column(db.Boolean)
    deactivate_time = db.Column(db.DateTime)
    deactivation_reason = db.Column(db.String())

    def __init__(self, user_id, channel_name, channel_topic, full_explanation):
        self.user_id = user_id
        self.channel_name = channel_name
        self.channel_topic = channel_topic
        self.full_explanation = full_explanation
        self.active = True

    def __repr__(self):
        return f"<id {self.id}, uuid {self.uid}>"


# Misc Slack Stuff

SLACK_MAX_TEXT_LENGTH = 3000
MAX_CHANNEL_NAME_LENGTH = 80
MAX_CHANNEL_TOPIC_LENGTH = 250


def invite_all(channel):
    try:
        all_users = client.users_list()["members"]
        users_already_in = client.conversations_members(channel=channel)["members"]
        # TODO: Handle pagination
        active_user_ids = [user["id"] for user in all_users if not user["deleted"]]
        already_in_ids = [user["id"] for user in users_already_in]
        inviteable_ids = [id for id in active_user_ids if id not in already_in_ids]
        # TODO: Probably need to make sure bot is in channel before inviting others
        # TODO: Probably need to remove bot from invite list
        client.conversations_invite(channel=channel, users=inviteable_ids)
    except SlackApiError as err:
        logging.error(err)
        pass  # TODO: something more


def format_datetime(dt, format="{date_num} {time_secs}", link=""):
    """Takes a python datetime object and returns a Slack formatted string"""
    unix_time = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    fallback_text = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    link_text = "^" + link if link else ""
    return f"<!date^{unix_time}^{format}{link_text}|{fallback_text}>"


# Slack Events


@slack_events_adapter.on("team_join")
def onboarding_message(payload):
    event = payload.get("event", {})
    user_id = event.get("user", {}).get("id")
    response = client.im_open(user=user_id)
    channel = response["channel"]["id"]
    # TODO: send them a message explaining The Gist
    # TODO: Invite them to the #ballotbox
    # TODO: Notify them of any in-progress votes
    # TODO: Invite them to the most recent posted theme


@slack_events_adapter.on("app_mention")
def app_mention(payload):
    event = payload["event"]
    channel = event["channel"]
    user = event["user"]
    user_name = client.users_info(user=user)["user"]["name"]
    try:
        client.chat_postMessage(
            channel=channel, text=f"keep my name out ya mouth, {user_name}"
        )
    except SlackApiError as err:
        logging.error(err)
        # TODO: something more


# Slash Commands


@app.route("/slack/command/submit", methods=["POST"])
def slash_submit():
    """Send the theme submission form in response to a /submit command"""
    ts = flask.request.headers.get("X-Slack-Request-Timestamp")
    sig = flask.request.headers.get("X-Slack-Signature")
    if not slack_events_adapter.server.verify_signature(ts, sig):
        logging.warn(f"Invalid message sent to /submit: ts={ts}, sig={sig}")
        flask.abort(400)
    trigger_id = flask.request.form["trigger_id"]
    try:
        client.dialog_open(trigger_id=trigger_id, dialog=SUBMIT_DIALOG)
    except SlackApiError as err:
        logging.error(err)
        # TODO: Something more
    return flask.make_response("", http.HTTPStatus.OK)


@app.route("/slack/command/submissions", methods=["POST"])
def slash_submissions():
    """Misc submission maintenance functions"""
    ts = flask.request.headers.get("X-Slack-Request-Timestamp")
    sig = flask.request.headers.get("X-Slack-Signature")
    if not slack_events_adapter.server.verify_signature(ts, sig):
        logging.warn(f"Invalid message sent to /submissions: ts={ts}, sig={sig}")
        flask.abort(400)

    form = flask.request.form
    text = form["text"]
    args = text.split()
    if not args or args[0] == "help":
        pass  # Do help thing here
    elif args[0] == "list":
        response = make_slash_submission_list_response(args, form)
    elif args[0] == "delete":
        response = make_slash_submission_delete_response(args, form)
    else:
        # TODO: Better error handling
        logging.error(f"Unknown arguments to command /submissions: {text}")
        response = flask.make_response("", 200)

    return response


def make_slash_submission_list_response(args, __):
    """List all submissions"""
    submissions = db.session.query(Submission).all()
    if submissions:
        response_text = ""
        i = 0
        n = len(submissions)
        for sub in submissions:
            i += 1
            sub_text = textwrap.dedent(
                f"""\
                *({i}/{n})*
                *UUID:* {sub.uid}
                *Submitted by:* <@{sub.user_id}>
                *Submitted at:* {format_datetime(sub.submission_time)}
                *Channel Name:* #{sub.channel_name}

                *Channel Topic:* {sub.channel_topic}

                *Full Explanation:* {sub.full_explanation}

                """
            )
            response_text += sub_text
    else:
        response_text = "The submission list is empty, ya fuckin layabouts"

    # TODO: Split this into multiple messages if over length limit
    # TODO: Use blocks to make this fancy
    if len(response_text) > SLACK_MAX_TEXT_LENGTH:
        logging.error(
            textwrap.dedent(
                f"""\
                Response longer than allowed \
                ({len(response_text)} > {SLACK_MAX_TEXT_LENGTH}
                {response_text}
            """
            )
        )
    return flask.make_response(
        flask.jsonify({"response_type": "ephemeral", "text": response_text}),
        http.HTTPStatus.OK,
    )


def make_slash_submission_delete_response(args, form):

    # TODO: Verify # args
    uuid_to_delete = args[1]

    try:
        submission = Submission.query.filter_by(uid=uuid_to_delete).first()
    except sqlalchemy.exc.SQLAlchemyError as err:
        logging.error(err)  # TODO: something here

    if submission.user_id != form["user_id"]:
        response_text = f"*Error:* Permission denied"
        logging.error(
            f"User {form['user_id']} lacks permissions to delete {uuid_to_delete}"
        )
        # TODO: Prettify this error
    else:
        try:
            response_text = (
                f"We're better off without #{submission.channel_name} anyhow"
            )
            print(response_text)
            db.session.delete(submission)
            db.session.commit()
        except sqlalchemy.exc.SQLAlchemyError as err:
            response_text = f"Error:\n{err}"  # TODO: something better

    return flask.make_response(
        flask.jsonify({"response_type": "ephemeral", "text": response_text}),
        http.HTTPStatus.OK,
    )


# Interactivity


@app.route("/slack/interactivity", methods=["POST"])
def interactivity():
    """This is the top-level "interactivity" handler for all dialog/view events"""
    ts = flask.request.headers.get("X-Slack-Request-Timestamp")
    sig = flask.request.headers.get("X-Slack-Signature")
    if not slack_events_adapter.server.verify_signature(ts, sig):
        logging.error("Aborting!")  # TODO: make this log message better
        flask.abort(http.HTTPStatus.BAD_REQUEST)
    payload = json.loads(flask.request.form["payload"])
    callback_id = payload["callback_id"]
    response = CALLBACK_IDS.get(callback_id, "unknown_payload")(payload)
    return response


def handle_submit_dialog(payload):
    channel_name = payload["submission"]["channel_name"]
    channel_name = channel_name.lower()

    try:
        # TODO: handle pagination
        channels = client.conversations_list(types="public_channel,private_channel")
    except SlackApiError as err:
        logging.error(err)
        # TODO: something more
    existing_channel_names = [c["name"] for c in channels["channels"]]

    # TODO: What if the name has already been submitted?
    if not re.match("^[a-z0-9-_.]+$", channel_name):
        error = {"name": "channel_name", "error": "Name contains prohibited characters"}
    elif channel_name in existing_channel_names:
        error = {"name": "channel_name", "error": "Name already exists"}
    elif channel_name in RESERVED_CHANNEL_NAMES:
        error = {"name": "channel_name", "error": "Name is a reserved channel name"}
    else:
        error = None
        user_id = payload["user"]["id"]
        channel_topic = payload["submission"]["channel_topic"]
        full_explanation = payload["submission"]["full_explanation"]
        try:
            db.session.add(
                Submission(user_id, channel_name, channel_topic, full_explanation)
            )
            db.session.commit()
        except sqlalchemy.exc.SQLAlchemyError as err:
            logging.error(err)
            error = {"name": "sqlalchemy", "error": "err"}

    if error is None:
        response = ""
    else:
        response = flask.jsonify({"errors": [error]})

    return flask.make_response(response, http.HTTPStatus.OK)


def handle_unknown_payload(payload):
    logging.error(f"Unknown payload received:\n{payload}")  # TODO: Pretty print
    return flask.make_response("", http.HTTPStatus.OK)


# Election Stuff
class Election:
    def init(
        self, candidates, channel=None, start_time=None, end_time=None, announce_text=""
    ):
        self._candidates = candidates  # TODO Update this to use the UUID scheme
        self._channel = channel
        self._start_time = start_time
        self._end_time = end_time
        self._announce_text = announce_text

        self._announce_message = None
        self._candidate_messages = {}

        self._winner = None

    def open_polls(self):
        """This method makes a base post announcing that voting has begun and replies
        to that post for each of the candidates.
        """

        try:
            self._announce_message = client.chat_postMessage(
                channel=self._channel, text=self._announce_text
            )["message"]
        except SlackApiError as err:
            logging.error(err)
            # TODO: something more

        timestamp = self._announce_message["ts"]
        i = 0
        for uid, value in self._candidates.items():
            i += 1
            message_text = (
                f"{i} of {len(self._candidates)}\n"
                f"{value['name']}\n"
                f"{value['description']}"
            )
            try:
                self._candidate_messages[uid] = client.chat_postMessage(
                    channel=timestamp, text=message_text
                )["message"]
            except SlackApiError as err:
                logging.error(err)
                # TODO: Something more
            # TODO: note that candidate has been voted on

    def close_polls(self):
        vote_count = self.tally_votes()
        self._winner = self.decide_winner(vote_count)

    def tally_votes(self):
        """Count number of unique voters for each candidate"""
        vote_count = {}
        for uid, message in self._candidate_messages.items():
            timestamp = message["ts"]
            try:
                reactions = client.reactions_get(
                    channel=self._channel, timestamp=timestamp, full=True
                )["message"]
            except SlackApiError as err:
                logging.error(err)
                # TODO: something more
            n_votes = len(
                set(
                    itertools.chain.from_iterable(
                        [r["users"] for r in reactions["reactions"]]
                    )
                )
            )
            vote_count[uid] = n_votes
        return vote_count

    @staticmethod
    def decide_winner(vote_count):
        """Find the candidate with the most votes. Ties are decided at random"""
        highest_vote = max(vote_count.values())
        winners = [uid for uid, votes in vote_count.items() if votes == highest_vote]
        return random.choice(winners)
        # TODO: other methods of deciding ties


def create_theme_channel(submission):
    name = submission["channel_name"]
    topic = submission["channel_topic"]
    try:
        channel = client.conversations_create(name=name)["channel"]  # TODO: Add users
    except SlackApiError as err:
        logging.error(err)
        # TODO something more

    try:
        client.conversations_setTopic(channel=channel["id"], topic=topic)
    except SlackApiError as err:
        logging.error(err)
        # TODO: something more

    try:
        author = submission["user_id"]
        date = submission["submission_time"]  # TODO format date
        purpose = f"submitted by {author} on {date}"
        # TODO: validate purpose length (21 chars max)
        client.conversations_setPurpose(channel=channel["id"], purpose=purpose)
        # TODO: it would be fun to post all the emoji used in the winning vote here
    except SlackApiError as err:
        logging.error(err)
        # TODO: something more


def select_candidates_for_election(n=3):
    """Select n candidates from the total list"""

    field = get_submissions()  # TODO: update
    if len(field) <= n:
        candidates = field
    else:
        candidates = random.sample(field, k)
        # TODO: Weight samples

    return candidates


SUBMIT_DIALOG = {
    "callback_id": "submit_dialog",
    "title": "Theme Submission",
    "submit_label": "Submit",
    "notify_on_cancel": False,
    "elements": [
        {
            "label": "Channel Name",
            "name": "channel_name",
            "type": "text",
            "placeholder": "weiners",
            "hint": "Only use lowercase letters, numbers, periods, dashes, underscores. Omit the leading #",
            "max_length": MAX_CHANNEL_NAME_LENGTH,
        },
        {
            "label": "Channel Topic",
            "name": "channel_topic",
            "type": "textarea",
            "placeholder": "Assuming you buy enough votes, this will be the channel topic",
            "max_length": MAX_CHANNEL_TOPIC_LENGTH,
        },
        {
            "label": "Full Explanation",
            "name": "full_explanation",
            "type": "textarea",
            "placeholder": "If present, this will be shown instead of the channel topic text in the voting post",
            "optional": True,
        },
    ],
}

RESERVED_CHANNEL_NAMES = [
    "aquí",
    "canais",
    "canal",
    "eu",
    "geral",
    "grupo",
    "mí",
    "todos",
    "archive",
    "archived",
    "archives",
    "all",
    "channel",
    "channels",
    "create",
    "delete",
    "deleted-channel",
    "edit",
    "everyone",
    "group",
    "groups",
    "here",
    "me",
    "ms",
    "slack",
    "slackbot",
    "today",
    "you",
    "chaîne",
    "chaine",
    "canal",
    "groupe",
    "ici",
    "moi",
    "tous",
    "alle",
    "allgemein",
    "Channel",
    "channel",
    "hier",
    "Gruppe",
    "gruppe",
    "mir",
    "チャンネル",
    "ここ",
    "全員",
    "自分",
    "グループ",
    "aquí",
    "canal",
    "grupo",
    "mí",
    "todos",
]

CALLBACK_IDS = {
    "submit_dialog": handle_submit_dialog,
    "unknown_payload": handle_unknown_payload,
}

if __name__ == "__main__":
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())
    # TODO: Be sure to print errors to console
    app.run(port=3000)
