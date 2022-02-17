# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies and contributors
# License: MIT. See LICENSE


from datetime import timedelta
from urllib.parse import quote
from icalendar import Calendar, Event
import uuid

import google.oauth2.credentials
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import frappe
from frappe import _
from frappe.integrations.doctype.google_settings.google_settings import get_auth_url
from frappe.model.document import Document
from frappe.utils import (add_days, add_to_date, get_datetime,
	get_request_site_address, get_time_zone, get_weekdays, now_datetime, format_datetime, getdate)


from frappe.utils.background_jobs import enqueue
from frappe.utils.scheduler import is_scheduler_inactive
from frappe.core.page.background_jobs.background_jobs import get_info

SCOPES = "https://www.googleapis.com/auth/calendar"

google_calendar_frequencies = {
	"RRULE:FREQ=DAILY": "Daily",
	"RRULE:FREQ=WEEKLY": "Weekly",
	"RRULE:FREQ=MONTHLY": "Monthly",
	"RRULE:FREQ=YEARLY": "Yearly"
}

google_calendar_days = {
	"MO": "monday",
	"TU": "tuesday",
	"WE": "wednesday",
	"TH": "thursday",
	"FR": "friday",
	"SA": "saturday",
	"SU": "sunday"
}

framework_frequencies = {
	"Daily": {"FREQ":"DAILY"},
	"Weekly": {"FREQ":"WEEKLY"},
	"Monthly": {"FREQ":"MONTHLY"},
	"Yearly": {"FREQ":"FREQ=YEARLY"}
}

framework_days = {
	"monday": "MO",
	"tuesday": "TU",
	"wednesday": "WE",
	"thursday": "TH",
	"friday": "FR",
	"saturday": "SA",
	"sunday": "SU"
}


class GoogleCalendar(Document):

	def validate(self):
		google_settings = frappe.get_single("Google Settings")
		if not google_settings.enable:
			frappe.throw(_("Enable Google API in Google Settings."))

		if not google_settings.client_id or not google_settings.client_secret:
			frappe.throw(_("Enter Client Id and Client Secret in Google Settings."))

		return google_settings

	def get_access_token(self):
		google_settings = self.validate()

		if not self.refresh_token:
			button_label = frappe.bold(_("Allow Google Calendar Access"))
			raise frappe.ValidationError(_("Click on {0} to generate Refresh Token.").format(button_label))

		data = {
			"client_id": google_settings.client_id,
			"client_secret": google_settings.get_password(fieldname="client_secret", raise_exception=False),
			"refresh_token": self.get_password(fieldname="refresh_token", raise_exception=False),
			"grant_type": "refresh_token",
			"scope": SCOPES
		}

		try:
			r = requests.post(get_auth_url(), data=data).json()
		except requests.exceptions.HTTPError:
			button_label = frappe.bold(_("Allow Google Calendar Access"))
			frappe.throw(_("Something went wrong during the token generation. Click on {0} to generate a new one.").format(button_label))

		return r.get("access_token")


@frappe.whitelist()
def authorize_access(g_calendar, reauthorize=None):
	"""
	If no Authorization code get it from Google and then request for Refresh Token.
	Google Calendar Name is set to flags to set_value after Authorization Code is obtained.
	"""
	google_settings = frappe.get_doc("Google Settings")
	google_calendar = frappe.get_doc("Google Calendar", g_calendar)

	redirect_uri = get_request_site_address(True) + "?cmd=frappe.integrations.doctype.google_calendar.google_calendar.google_callback"

	if not google_calendar.authorization_code or reauthorize:
		frappe.cache().hset("google_calendar", "google_calendar", google_calendar.name)
		return get_authentication_url(client_id=google_settings.client_id, redirect_uri=redirect_uri)
	else:
		try:
			data = {
				"code": google_calendar.get_password(fieldname="authorization_code", raise_exception=False),
				"client_id": google_settings.client_id,
				"client_secret": google_settings.get_password(fieldname="client_secret", raise_exception=False),
				"redirect_uri": redirect_uri,
				"grant_type": "authorization_code"
			}
			r = requests.post(get_auth_url(), data=data).json()

			if "refresh_token" in r:
				frappe.db.set_value("Google Calendar", google_calendar.name, "refresh_token", r.get("refresh_token"))
				frappe.db.commit()

			frappe.local.response["type"] = "redirect"
			frappe.local.response["location"] = "/app/Form/{0}/{1}".format(quote("Google Calendar"), quote(google_calendar.name))

			frappe.msgprint(_("Google Calendar has been configured."))
		except Exception as e:
			frappe.throw(e)


def get_authentication_url(client_id=None, redirect_uri=None):
	return {
		"url": "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&response_type=code&prompt=consent&client_id={}&include_granted_scopes=true&scope={}&redirect_uri={}".format(client_id, SCOPES, redirect_uri)
	}

@frappe.whitelist()
def google_callback(code=None):
	"""
	Authorization code is sent to callback as per the API configuration
	"""
	google_calendar = frappe.cache().hget("google_calendar", "google_calendar")
	frappe.db.set_value("Google Calendar", google_calendar, "authorization_code", code)
	frappe.db.commit()

	authorize_access(google_calendar)


@frappe.whitelist()
def sync(g_calendar=None):
	filters = {"enable": 1}

	if g_calendar:
		filters.update({"name": g_calendar})

	google_calendars = frappe.get_list("Google Calendar", filters=filters)

	for g in google_calendars:
		return sync_events_from_google_calendar(g.name)

def get_google_calendar_object(g_calendar):
	"""
	Returns an object of Google Calendar along with Google Calendar doc.
	"""
	google_settings = frappe.get_doc("Google Settings")
	account = frappe.get_doc("Google Calendar", g_calendar)

	credentials_dict = {
		"token": account.get_access_token(),
		"refresh_token": account.get_password(fieldname="refresh_token", raise_exception=False),
		"token_uri": get_auth_url(),
		"client_id": google_settings.client_id,
		"client_secret": google_settings.get_password(fieldname="client_secret", raise_exception=False),
		"scopes": "https://www.googleapis.com/auth/calendar/v3"
	}

	credentials = google.oauth2.credentials.Credentials(**credentials_dict)
	google_calendar = build(
		serviceName="calendar",
		version="v3",
		credentials=credentials,
		static_discovery=False
	)

	check_google_calendar(account, google_calendar)

	account.load_from_db()
	return account


def check_google_calendar(account, google_calendar):
	"""
	Checks if Google Calendar is present with the specified name.
	If not, creates one.
	"""
	account.load_from_db()
	try:
		if account.google_calendar_id:
			google_calendar.calendars().get(calendarId=account.google_calendar_id).execute()
		else:
			# If no Calendar ID create a new Calendar
			calendar = {
				"summary": account.calendar_name,
				"timeZone": frappe.db.get_single_value("System Settings", "time_zone")
			}
			created_calendar = google_calendar.calendars().insert(body=calendar).execute()
			frappe.db.set_value("Google Calendar", account.name, "google_calendar_id", created_calendar.get("id"))
			frappe.db.commit()
	except HttpError as err:
		frappe.throw(_("Google Calendar - Could not create Calendar for {0}, error code {1}.").format(account.name, err.resp.status))


def sync_events_from_google_calendar(g_calendar, method=None):
	"""
	Syncs Events from Google Calendar in Framework Calendar.
	Google Calendar returns nextSyncToken when all the events in Google Calendar are fetched.
	nextSyncToken is returned at the very last page
	https://developers.google.com/calendar/v3/sync
	"""
	account = get_google_calendar_object(g_calendar)

	account = frappe.get_doc("Google Calendar", g_calendar)
	caldav_url = "https://apidata.googleusercontent.com/caldav/v2/{}/events?access_token={}".format(account.user,account.get_access_token())
	response = requests.get(url=caldav_url, headers={"User-Agent": "Mozilla/5.0","Content-Type": "text/xml","Accept": "text/xml, text/calendar"})
	cal = Calendar.from_ical(response.content)
	enqueue_job(parse_calendar_events,calendar = cal,account = account)

def parse_calendar_events(calendar,account):
	event_list =[]
	for event in calendar.walk('vevent'):
		recurrence = None
		event_id = get_event_id(event)
		if event.get("RRULE"):
			recurrence = event["RRULE"]
		if event["STATUS"] == "CONFIRMED":
			attendees = get_event_attendees(event)
			event_list.append(event_id)
			if not frappe.db.exists("Event", {"google_calendar_event_id": event_id}):
				insert_event_to_calendar(account, event, attendees,recurrence)
			else:
				update_event_in_calendar(account, event, attendees,recurrence)
	close_cancelled_events(event_list)


def get_event_id(event):
	return "{}|{}".format(event["SUMMARY"],(event["DTSTART"].dt).strftime("%Y-%m-%d %H:%M"))

def insert_event_to_calendar(account, event,attendees, recurrence=None):
	"""
	Inserts event in Frappe Calendar during Sync
	"""
	calendar_event = {
		"doctype": "Event",
		"subject": event.get("SUMMARY") or _("No Subject"),
		"description": event.get("DESCRIPTION"),
		"google_calendar_event": 1,
		"google_calendar": account.name,
		"google_calendar_id": account.google_calendar_id,
		"google_calendar_event_id": get_event_id(event),
		"pulled_from_google_calendar": 1
	}
	calendar_event.update(google_calendar_to_repeat_on(recurrence=recurrence, start=event.get("DTSTART").dt, end= (event.get("DTEND").dt if event.get("DTEND") else None)))
	calendar_event.update({"event_participants":attendees})
	frappe.get_doc(calendar_event).insert(ignore_permissions=True)


def update_event_in_calendar(account, update_event, attendees,recurrence=None):
	"""
	Updates Event in Frappe Calendar if any existing Google Calendar Event is updated
	"""
	calendar_event = frappe.get_doc("Event", {"google_calendar_event_id": get_event_id(update_event)})
	update_content = False
	if calendar_event.subject != update_event.get("SUMMARY"):
		calendar_event.subject != update_event.get("SUMMARY")
		update_content = True
	if calendar_event.description != update_event.get("DESCRIPTION"):
		calendar_event.description != update_event.get("DESCRIPTION")
		update_content = True
	calendar_event.update(google_calendar_to_repeat_on(recurrence=recurrence, start=update_event.get("DTSTART").dt, end=update_event.get("DTEND").dt))
	# calendar_event.set("event_participants",attendees)
	if update_content:
		calendar_event.save(ignore_permissions=True)

def close_cancelled_events(confirmed_list):
	event_doc = frappe.qb.DocType('Event') 
	(frappe.qb.update(event_doc).set(event_doc.status , "Closed").where(event_doc.google_calendar_event_id.notin(confirmed_list))).run()

def insert_event_in_google_calendar(doc, method=None):
	"""
	Insert Events in Google Calendar if sync_with_google_calendar is checked.
	"""
	if not frappe.db.exists("Google Calendar", {"name": doc.google_calendar}) or doc.pulled_from_google_calendar \
		or not doc.sync_with_google_calendar:
		return

	account = get_google_calendar_object(doc.google_calendar)

	if not account.push_to_google_calendar:
		return

	event = {
		"summary": doc.subject,
		"description": doc.description,
		"google_calendar_event": 1
	}
	event.update(format_date_according_to_google_calendar(doc.all_day, get_datetime(doc.starts_on), get_datetime(doc.ends_on)))

	if doc.repeat_on:
		event.update({"recurrence": repeat_on_to_google_calendar_recurrence_rule(doc)})

	caldav_url, cal = create_calendar_object(account,doc)
	event = Event()
	event['DTSTART'] = format_datetime(doc.starts_on, "yyyyMMddTHHmmss")
	event['DTEND'] = format_datetime(doc.ends_on, "yyyyMMddTHHmmss")
	event['SUMMARY'] = doc.subject
	event['UID'] = uuid.uuid4()
	event['DESCRIPTION'] = doc.description
	
	for guest in doc.get('event_participants'):
		event['attendee'].append('MAILTO:{}'.format(guest['reference_docname']))
	
	if doc.repeat_on:
		event['RRULE'] = repeat_on_to_google_calendar_recurrence_rule(doc)
	cal.add_component(event)
	response = requests.put(url = caldav_url,data=cal.to_ical() , headers={"Content-Type": 'text/calendar; charset="utf-8"'})
	if response.status_code == 201:
		frappe.db.set_value("Event", doc.name, "google_calendar_event_id", event['UID'], update_modified=False)
		frappe.msgprint(_("Event Synced with Google Calendar."))
	else :
		frappe.throw(_("Google Calendar - Could not insert event in Google Calendar {0}, error code {1}.").format(account.name, response.text))

def update_event_in_google_calendar(doc, method=None):
	"""
	Updates Events in Google Calendar if any existing event is modified in Frappe Calendar
	"""
	# Workaround to avoid triggering updation when Event is being inserted since
	# creation and modified are same when inserting doc
	if not frappe.db.exists("Google Calendar", {"name": doc.google_calendar}) or doc.modified == doc.creation \
		or not doc.sync_with_google_calendar:
		return

	if doc.sync_with_google_calendar and not doc.google_calendar_event_id:
		# If sync_with_google_calendar is checked later, then insert the event rather than updating it.
		insert_event_in_google_calendar(doc)
		return

	account = get_google_calendar_object(doc.google_calendar)

	if not account.push_to_google_calendar:
		return
	repeat_on_to_google_calendar_recurrence_rule(doc)
	try:
		caldav_url, cal = create_calendar_object(account,doc)
		event = Event()
		event['DTSTART'] = format_datetime(doc.starts_on, "yyyyMMddTHHmmss")
		event['DTEND'] = format_datetime(doc.ends_on, "yyyyMMddTHHmmss")
		event['SUMMARY'] = doc.subject
		event['UID'] = doc.google_calendar_event_id
		event['DESCRIPTION'] = doc.description
		event['ATTENDEE'] =[]
		if doc.repeat_on:
			event['RRULE']=repeat_on_to_google_calendar_recurrence_rule(doc)

		for guest in doc.get('event_participants'):
			contact_doc = frappe.get_doc(guest.get('reference_doctype'), guest.get('reference_docname'))
			event['ATTENDEE'].append('MAILTO:{}'.format(contact_doc.email_id))
		cal.add_component(event)
		response = requests.put(url = caldav_url,data=cal.to_ical() , headers={"Content-Type": 'text/calendar; charset="utf-8"'})
		if response.status_code == 204:
			frappe.msgprint(_("Event Synced with Google Calendar."))
		else :
			frappe.throw(_("Google Calendar - Could not update Event {0} in Google Calendar, error code {1}.").format(doc.name, response.status_code))
	except HttpError as err:
		frappe.throw(_("Google Calendar - Could not update Event {0} in Google Calendar, error code {1}.").format(doc.name, err.resp.status))


def delete_event_from_google_calendar(doc, method=None):
	"""
	Delete Events from Google Calendar if Frappe Event is deleted.
	"""

	if not frappe.db.exists("Google Calendar", {"name": doc.google_calendar}):
		return

	account = get_google_calendar_object(doc.google_calendar)

	if not account.push_to_google_calendar:
		return

	try:
		caldav_url, cal = create_calendar_object(account,doc)
		cal["METHOD"] = "CANCEL" 
		event = Event()
		event['DTSTART'] = format_datetime(doc.starts_on, "yyyyMMddTHHmmss")
		event['DTEND'] = format_datetime(doc.ends_on, "yyyyMMddTHHmmss")
		event['SUMMARY'] = doc.subject
		event["UID"] = doc.google_calendar_event_id
		event["STATUS"] = "CANCELLED"
		cal.add_component(event)
		requests.delete(url = caldav_url,data=cal.to_ical()  , headers={"Content-Type": 'text/calendar; charset="utf-8"'})
	except HttpError as err:
		frappe.msgprint(_("Google Calendar - Could not delete Event {0} from Google Calendar, error code {1}.").format(doc.name, err.resp.status))


def google_calendar_to_repeat_on(start, end, recurrence=None):
	"""
	recurrence is in the form ['RRULE:FREQ=WEEKLY;WKST=SU;UNTIL=20210912T182959Z;BYDAY=FR,MO,WE']
	has the frequency and then the days on which the event recurs
	Both have been mapped in a dict for easier mapping.
	"""
	starts_on = (get_datetime(start) if start else get_datetime(start)).replace(tzinfo=None)
	ends_on = (get_datetime(end) if end else get_datetime(end)).replace(tzinfo=None)

	repeat_on = {
		"starts_on": starts_on,
		"ends_on": ends_on,
		"all_day": 0 if end else 1,
		"repeat_this_event": 1 if end else 0,
		"repeat_on": None,
		"repeat_till": None,
		"sunday": 0,
		"monday": 0,
		"tuesday": 0,
		"wednesday": 0,
		"thursday": 0,
		"friday": 0,
		"saturday": 0,
	}

	if recurrence:
		google_calendar_frequency, until, byday = get_recurrence_parameters(recurrence)
		repeat_on["repeat_on"] = google_calendar_frequencies.get(google_calendar_frequency)

		if repeat_on["repeat_on"] == "Daily":
			repeat_on["ends_on"] = None
			repeat_on["repeat_till"] = until

		if byday and repeat_on["repeat_on"] == "Weekly":
			repeat_on["repeat_till"] = until
			byday = byday.split("=")[1].split(",")
			for repeat_day in byday:
				repeat_on[google_calendar_days[repeat_day]] = 1

		if byday and repeat_on["repeat_on"] == "Monthly":
			byday = byday.split("=")[1]
			repeat_day_week_number, repeat_day_name = None, None

			for num in ["-2", "-1", "1", "2", "3", "4", "5"]:
				if num in byday:
					repeat_day_week_number = num
					break

			for day in ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]:
				if day in byday:
					repeat_day_name = google_calendar_days.get(day)
					break

			# Only Set starts_on for the event to repeat monthly
			start_date = parse_google_calendar_recurrence_rule(int(repeat_day_week_number), repeat_day_name)
			repeat_on["starts_on"] = start_date
			repeat_on["ends_on"] = add_to_date(start_date, minutes=5)
			repeat_on["repeat_till"] = until

		if repeat_on["repeat_till"] == "Yearly":
			repeat_on["ends_on"] = None
			repeat_on["repeat_till"] = until

	return repeat_on


def format_date_according_to_google_calendar(all_day, starts_on, ends_on=None):
	if not ends_on:
		ends_on = starts_on + timedelta(minutes=10)

	date_format = {
		"start": {
			"dateTime": starts_on.isoformat(),
			"timeZone": get_time_zone(),
		},
		"end": {
			"dateTime": ends_on.isoformat(),
			"timeZone": get_time_zone(),
		}
	}

	if all_day:
		# If all_day event, Google Calendar takes date as a parameter and not dateTime
		date_format["start"].pop("dateTime")
		date_format["end"].pop("dateTime")

		date_format["start"].update({"date": starts_on.date().isoformat()})
		date_format["end"].update({"date": ends_on.date().isoformat()})

	return date_format


def parse_google_calendar_recurrence_rule(repeat_day_week_number, repeat_day_name):
	"""
	Returns (repeat_on) exact date for combination eg 4TH viz. 4th thursday of a month
	"""
	if repeat_day_week_number < 0:
		# Consider a month with 5 weeks and event is to be repeated in last week of every month, google caledar considers
		# a month has 4 weeks and hence itll return -1 for a month with 5 weeks.
		repeat_day_week_number = 4

	weekdays = get_weekdays()
	current_date = now_datetime()
	isset_day_name, isset_day_number = False, False

	# Set the proper day ie if recurrence is 4TH, then align the day to Thursday
	while not isset_day_name:
		isset_day_name = True if weekdays[current_date.weekday()].lower() == repeat_day_name else False
		current_date = add_days(current_date, 1) if not isset_day_name else current_date

	# One the day is set to Thursday, now set the week number ie 4
	while not isset_day_number:
		week_number = get_week_number(current_date)
		isset_day_number = True if week_number == repeat_day_week_number else False
		# check if  current_date week number is greater or smaller than repeat_day week number
		weeks = 1 if week_number < repeat_day_week_number else -1
		current_date = add_to_date(current_date, weeks=weeks) if not isset_day_number else current_date

	return current_date


def repeat_on_to_google_calendar_recurrence_rule(doc):
	"""
	Returns event (repeat_on) in Google Calendar format ie RRULE:FREQ=WEEKLY;BYDAY=MO,TU,TH
	"""
	recurrence = framework_frequencies.get(doc.repeat_on)
	weekdays = get_weekdays()
	if doc.repeat_on == "Weekly":
		byday = [framework_days.get(day.lower()) for day in weekdays if doc.get(day.lower())]
		recurrence["BYDAY"] = byday
	elif doc.repeat_on == "Monthly":
		week_number = str(get_week_number(get_datetime(doc.starts_on)))
		week_day = weekdays[get_datetime(doc.starts_on).weekday()].lower()
		recurrence["BYDAY"] = week_number + framework_days.get(week_day)
	if doc.repeat_till is not None:
		recurrence["UNTIL"] = getdate(doc.repeat_till)
	return recurrence


def get_week_number(dt):
	"""
	Returns the week number of the month for the specified date.
	https://stackoverflow.com/questions/3806473/python-week-number-of-the-month/16804556
	"""
	from math import ceil

	first_day = dt.replace(day=1)

	dom = dt.day
	adjusted_dom = dom + first_day.weekday()

	return int(ceil(adjusted_dom / 7.0))


def get_recurrence_parameters(recurrence):
	frequency, until, byday = None, None, None

	for r in recurrence:
		if "FREQ" in r:
			frequency = r
		elif "UNTIL" in r:
			until = r
		elif "BYDAY" in r:
			byday = r
		else:
			pass

	return frequency, until, byday

def get_event_attendees(event):
	event_participants = []
	for item in event.get("ATTENDEE",[]):
		attendee_email = item.removeprefix("mailto:")
		if frappe.db.exists('Contact', {'email_id': attendee_email}) :
			event_participants.append({"reference_doctype":"Contact","reference_docname":frappe.get_doc("Contact", {'email_id': attendee_email}).name})
		else:
			contact = {
				"doctype": "Contact",
				"first_name": attendee_email.split("@")[0],
				"email_id": attendee_email,
				"email_ids":[{"email_id":attendee_email,"is_primary":1}]
			}
			doc = frappe.get_doc(contact).insert(ignore_permissions=True)
			event_participants.append({"reference_doctype":"Contact","reference_docname":doc.name})
	return event_participants

def create_calendar_object(account, doc):
	caldav_url = "https://apidata.googleusercontent.com/caldav/v2/{}/events/{}?access_token={}".format(account.user,doc.name,account.get_access_token())
	cal = Calendar() 
	cal['PRODID'] = '-//Google Inc//Google Calendar 70.9054//EN'
	cal['VERSION'] = '2.0'
	cal['CALSCALE'] = account.user
	cal['X-WR-CALNAME'] = account.user
	cal['X-WR-TIMEZONE'] = frappe.db.get_single_value("System Settings", "time_zone")
	return caldav_url,cal 

def enqueue_job(job, **kwargs):
	check_scheduler_status() 
	account = kwargs.get('account') or {} 
	job_name = "calendar_import|{}".format(account.get("name"))
	if not job_already_enqueued(job_name):
		enqueue(job,**kwargs,queue="long",timeout=10000,event="Import events from google ",job_name=job_name,now=frappe.conf.developer_mode or frappe.flags.in_test)

def check_scheduler_status():
	if is_scheduler_inactive() and not frappe.flags.in_test:
		frappe.throw(_("Scheduler is inactive. Cannot enqueue job."), title=_("Scheduler Inactive"))

def job_already_enqueued(job_name):
	enqueued_jobs = [d.get("job_name") for d in get_info()]
	if job_name in enqueued_jobs:
		return True