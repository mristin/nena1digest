#!/usr/bin/env python
__author__ = "marko"

import sys, os, datetime, smtplib, json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import sqlalchemy
from sqlalchemy import and_

import jinja2

import model
from model import Forum_post, Forum_section, Forum_group, Forum_topic, User, Blog_post, Event
import configuration


def seconds_since_epoch(a_datetime):
    """
    :param a_datetime: datetime.datetime
    :return: long
    """
    """
    :param a_datetime: datetime.datetime
    :return: long
    """
    epoch = datetime.datetime.utcfromtimestamp(0)
    return long((a_datetime - epoch).total_seconds())


datetime_format = "%Y-%m-%dT%H-%M-%S"


def read_last_insterval_end():
    if not os.path.exists(os.path.dirname(configuration.state_path)):
        sys.stderr.write("The directory %s of the log file %s must exist." % (
            os.path.dirname(configuration.state_path), configuration.state_path))
        sys.exit(1)

    if not os.path.exists(configuration.state_path):
        fid = open(configuration.state_path, "w")
        fid.close()

    lines = []
    fid = open(configuration.state_path, "r")
    lines = [line for line in fid.readlines() if not line.startswith("#") and not len(line.strip()) == 0]
    fid.close()

    def extract_timestamps(enumerated_line):
        (line_number, line) = enumerated_line
        try:
            entry = json.loads(line)
            return datetime.datetime.strptime(entry["interval_end"], datetime_format)
        except Exception as exception:
            raise RuntimeError("Line %d in log file %s is invalid: %s." % (
                line_number, configuration.state_path, repr(exception)
            ))

    timestamps = [configuration.exclude_content_before] + map(extract_timestamps, enumerate(lines))
    return max(timestamps)


def update_state(path, entry):
    assert (path in [configuration.state_path, configuration.error_log_path])

    try:
        with open(configuration.state_path, "a") as fid:
            fid.write("\n" + json.dumps(entry))

    except IOError:
        sys.stderr.write("The state file %s could not be opened for appending." % configuration.state_path)
        sys.exit(1)


def digest():
    interval_begin = read_last_insterval_end()
    interval_end = datetime.datetime.now()

    engine = sqlalchemy.create_engine(configuration.database_url)

    model.Base.metadata.bind = engine
    DBSession = sqlalchemy.orm.sessionmaker(bind=engine)
    session = DBSession()

    # Cache
    session.query(Forum_section).all()
    session.query(Forum_group).all()
    session.query(Forum_topic).all()
    session.query(User).all()

    # Load template
    script_dir = os.path.dirname(os.path.realpath(__file__))
    template_path = os.path.join(script_dir, "mail_template.html")
    fid = open(template_path, "r")
    template = jinja2.Template(fid.read())
    fid.close()

    # Digest the forum content
    forum_posts = []
    for post in session.query(Forum_post).filter(
            and_(Forum_post.create_stamp >= seconds_since_epoch(interval_begin),
                 Forum_post.create_stamp < seconds_since_epoch(interval_end))).order_by(Forum_post.create_stamp):

        if not post.topic.group.is_private and not post.topic.group.section.is_hidden:
            post_dict = {
                "username": post.user.username.decode("UTF-8", errors="replace"),
                "date": datetime.datetime.fromtimestamp(post.create_stamp).strftime("%Y-%m-%d %H:%M"),
                "topic": post.topic.title.decode("UTF-8", errors="replace"),
                "url": "http://oxwall.nena1.ch/forum/topic/%s" % (post.topic.id)
            }

        forum_posts.append(post_dict)

    blog_posts = []
    for post in session.query(Blog_post).filter(
            and_(Blog_post.timestamp >= seconds_since_epoch(interval_begin),
                 Blog_post.timestamp < seconds_since_epoch(interval_end)),
                    Blog_post.privacy == "everybody",
                    Blog_post.is_draft == False).order_by(Blog_post.timestamp):
        blog_posts.append({
            "username": post.user.username.decode("UTF-8", errors="replace"),
            "date": datetime.datetime.fromtimestamp(post.timestamp).strftime("%Y-%m-%d %H:%M"),
            "title": post.title.decode("UTF-8", errors="replace"),
            "url": "http://oxwall.nena1.ch/blogs/%s" % (post.id)
        })

    events = []
    for event in session.query(Event).filter(
            and_(Event.create_timestamp >= seconds_since_epoch(interval_begin),
                 Event.create_timestamp < seconds_since_epoch(interval_end)),
                    Event.who_can_view == 1).order_by(Event.create_timestamp):
        events.append({
            "username": event.user.username.decode("UTF-8", errors="replace"),
            "date": datetime.datetime.fromtimestamp(event.create_timestamp).strftime("%Y-%m-%d %H:%M"),
            "title": event.title,
            "url": "http://oxwall.nena1.ch/blogs/%s" % (event.id)
        })

    message = template.render(forum_posts=forum_posts, blog_posts=blog_posts).encode("UTF-8")

    # Send digests
    if len(message) > configuration.max_message_size:
        raise RuntimeError("The message size (== %d) exeeds the maximum message size (== %d)." % (
            len(message), configuration.max_message_size))

    recipient_list = []
    if configuration.send_to_all_oxwall_users == True:
        for user in session.query(User).all():
            recipient_list.append(user.email)

    recipient_list.extend(configuration.additional_recipients)
    excluded_recipient_set = set(configuration.excluded_recipients)
    recipient_list = [recipient for recipient in recipient_list if recipient not in excluded_recipient_set]

    s = smtplib.SMTP(configuration.smtp_server)
    for recipient in recipient_list:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Nena1 Oxwall Zusammenfassung von %s bis %s" % (
            interval_begin.strftime("%Y-%m-%d %H:%M"),
            interval_end.strftime("%Y-%m-%d %H:%M")
        )
        msg["From"] = configuration.sender
        msg["To"] = recipient
        msg.attach(MIMEText(
            "Dein Email-Klient kann leider keine HTML-Nachrichten anzeigen. " + \
            "Bitte wende Dich an: %s" % (
                configuration.admin_email), "plain"))
        msg.attach(MIMEText("".join(message), "html"))

        s.sendmail(configuration.sender, recipient, msg.as_string())

    s.quit()

    # Update the checkpoint.
    update_state(configuration.state_path, {
        "interval_begin": interval_begin.strftime(datetime_format),
        "interval_end": interval_end.strftime(datetime_format),
        "forum_post_count": len(forum_posts),
        "blog_post_count": len(blog_posts),
        "event_count": len(events),
        "message_size": len(message),
        "recipient_count": len(recipient_list)})


def main():
    if len(sys.argv) != 1:
        sys.stderr.write("Usage: launch.py {no arguments}")
        sys.exit(1)

    try:
        digest()
    except Exception as e:
        update_state(configuration.error_log_path, {"error": str(e), "date": str(datetime.datetime.now())})


if __name__ == "__main__":
    main()
