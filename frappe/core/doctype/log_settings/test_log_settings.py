# Copyright (c) 2022, Frappe Technologies and Contributors
# License: MIT. See LICENSE

from datetime import datetime
import unittest

import frappe
from frappe.utils import now_datetime, add_to_date
from frappe.core.doctype.log_settings.log_settings import run_log_clean_up


class TestLogSettings(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.savepoint = "TestLogSettings"
		# SAVEPOINT can only be used in transaction blocks and we don't wan't to take chances
		frappe.db.begin()
		frappe.db.savepoint(cls.savepoint)

		frappe.db.set_single_value(
			"Log Settings",
			{
				"clear_error_log_after": 1,
				"clear_activity_log_after": 1,
				"clear_email_queue_after": 1,
			},
		)

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback(save_point=cls.savepoint)

	def setUp(self) -> None:
		if self._testMethodName == "test_delete_logs":
			self.datetime = frappe._dict()
			self.datetime.current = now_datetime()
			self.datetime.past = add_to_date(self.datetime.current, days=-4)
			setup_test_logs(self.datetime.past)

	def tearDown(self) -> None:
		if self._testMethodName == "test_delete_logs":
			del self.datetime

	def test_delete_logs(self):
		# make sure test data is present
		activity_log_count = frappe.db.count(
			"Activity Log", {"creation": ("<=", self.datetime.past)}
		)
		error_log_count = frappe.db.count(
			"Error Log", {"creation": ("<=", self.datetime.past)}
		)
		email_queue_count = frappe.db.count(
			"Email Queue", {"creation": ("<=", self.datetime.past)}
		)

		self.assertNotEqual(activity_log_count, 0)
		self.assertNotEqual(error_log_count, 0)
		self.assertNotEqual(email_queue_count, 0)

		# run clean up job
		run_log_clean_up()

		# test if logs are deleted
		activity_log_count = frappe.db.count(
			"Activity Log", {"creation": ("<", self.datetime.past)}
		)
		error_log_count = frappe.db.count(
			"Error Log", {"creation": ("<", self.datetime.past)}
		)
		email_queue_count = frappe.db.count(
			"Email Queue", {"creation": ("<", self.datetime.past)}
		)

		self.assertEqual(activity_log_count, 0)
		self.assertEqual(error_log_count, 0)
		self.assertEqual(email_queue_count, 0)


def setup_test_logs(past: datetime) -> None:
	activity_log = frappe.get_doc(
		{
			"doctype": "Activity Log",
			"subject": "Test subject",
			"full_name": "test user2",
		}
	).insert(ignore_permissions=True)
	activity_log.db_set("creation", past)

	error_log = frappe.get_doc(
		{
			"doctype": "Error Log",
			"method": "test_method",
			"error": "traceback",
		}
	).insert(ignore_permissions=True)
	error_log.db_set("creation", past)

	doc1 = frappe.get_doc(
		{
			"doctype": "Email Queue",
			"sender": "test1@example.com",
			"message": "This is a test email1",
			"priority": 1,
			"expose_recipients": "test@receiver.com",
		}
	).insert(ignore_permissions=True)
	doc1.db_set("creation", past)
