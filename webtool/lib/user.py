"""
User class
"""
import html2text
import hashlib
import smtplib
import socket
import bcrypt
import json
import time
import os

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from common.lib.helpers import send_email

import common.config_manager as config

class User:
	"""
	User class

	Compatible with Flask-Login
	"""
	data = {}
	is_authenticated = False
	is_active = False
	is_anonymous = True

	name = "anonymous"

	@staticmethod
	def get_by_login(db, name, password):
		"""
		Get user object, if login is correct

		If the login data supplied to this method is correct, a new user object
		that is marked as authenticated is returned.

		:param db:  Database connection object
		:param name:  User name
		:param password:  User password
		:return:  User object, or `None` if login was invalid
		"""
		user = db.fetchone("SELECT * FROM users WHERE name = %s", (name,))
		if not user or not user.get("password", None):
			# registration not finished yet
			return None
		elif not user or not bcrypt.checkpw(password.encode("ascii"), user["password"].encode("ascii")):
			# non-existing user or wrong password
			return None
		else:
			# valid login!
			return User(db, user, authenticated=True)

	@staticmethod
	def get_by_name(db, name):
		"""
		Get user object for given user name

		:param db:  Database connection object
		:param str name:  Username to get object for
		:return:  User object, or `None` for invalid user name
		"""
		user = db.fetchone("SELECT * FROM users WHERE name = %s", (name,))
		if not user:
			return None
		else:
			return User(db, user)

	@staticmethod
	def get_by_token(db, token):
		"""
		Get user object for given token, if token is valid

		:param db:  Database connection object
		:param str token:  Token to get object for
		:return:  User object, or `None` for invalid token
		"""
		user = db.fetchone(
			"SELECT * FROM users WHERE register_token = %s AND (timestamp_token = 0 OR timestamp_token > %s)",
			(token, int(time.time()) - (7 * 86400)))
		if not user:
			return None
		else:
			return User(db, user)

	def can_access_dataset(self, dataset):
		"""
		Check if this user should be able to access a given dataset.

		This depends mostly on the dataset's owner, which should match the
		user if the dataset is private. If the dataset is not private, or
		if the user is an admin or the dataset is private but assigned to
		an anonymous user, the dataset can be accessed.

		:param dataset:  The dataset to check access to
		:return bool:
		"""
		if not dataset.is_private:
			return True

		elif self.is_admin:
			return True

		elif self.get_id() == dataset.owner:
			return True

		elif dataset.owner == "anonymous":
			return True

		else:
			return False

	def __init__(self, db, data, authenticated=False):
		"""
		Instantiate user object

		Also sets the properties required by Flask-Login.

		:param db:  Database connection object
		:param data:  User data
		:param authenticated:  Whether the user should be marked as authenticated
		"""
		self.db = db
		self.data = data

		if self.data["name"] != "anonymous":
			self.is_anonymous = False
			self.is_active = True

		self.name = self.data["name"]
		self.is_authenticated = authenticated

		self.userdata = json.loads(self.data.get("userdata", "{}"))

		if not self.is_anonymous and self.is_authenticated:
			self.db.update("users", where={"name": self.data["name"]}, data={"timestamp_seen": int(time.time())})

	def authenticate(self):
		"""
		Mark user object as authenticated.
		"""
		self.is_authenticated = True

	def get_id(self):
		"""
		Get user ID

		:return:  User ID
		"""
		return self.data["name"]

	def get_name(self):
		"""
		Get user name

		This is usually the user ID. For the two special users, provide a nicer
		name to display in interfaces, etc.

		:return: User name
		"""
		if self.data["name"] == "anonymous":
			return "Anonymous"
		elif self.data["name"] == "autologin":
			return config.get("flask.autologin.name")
		else:
			return self.data["name"]

	def get_token(self):
		"""
		Get password reset token

		May be empty or invalid!

		:return str: Password reset token
		"""
		return self.generate_token(regenerate=False)

	def clear_token(self):
		"""
		Reset password rest token

		Clears the token and token timestamp. This allows requesting a new one
		even if the old one had not expired yet.

		:return:
		"""
		self.db.update("users", data={"register_token": "", "timestamp_token": 0}, where={"name": self.get_id()})

	@property
	def is_special(self):
		"""
		Check if user is special user

		:return:  Whether the user is the anonymous user, or the automatically
		logged in user.
		"""
		return self.get_id() in ("autologin", "anonymous")

	@property
	def is_admin(self):
		"""
		Check if user is an administrator

		:return bool:
		"""
		return self.data["is_admin"]

	@property
	def is_deactivated(self):
		"""
		Check if user has been deactivated

		:return bool:
		"""
		return self.data.get("is_deactivated", False)

	def get_attribute(self, attribute):
		return json.loads(self.data["userdata"]).get(attribute, None)

	def email_token(self, new=False):
		"""
		Send user an e-mail with a password reset link

		Generates a token that the user can use to reset their password. The
		token is valid for 72 hours.

		Note that this requires a mail server to be configured, or a
		`RuntimeError` will be raised. If a server is configured but the mail
		still fails to send, it will also raise a `RuntimeError`. Note that
		in these cases a token is still created and valid (the user just gets
		no notification, but an admin could forward the correct link).

		If the user is a 'special' user, a `ValueError` is raised.

		:param bool new:  Is this the first time setting a password for this
						  account?
		:return str:  Link for the user to set their password with
		"""
		if not config.get('mail.server'):
			raise RuntimeError("No e-mail server configured. 4CAT cannot send any e-mails.")

		if self.is_special:
			raise ValueError("Cannot send password reset e-mails for a special user.")

		username = self.get_id()

		# generate a password reset token
		register_token = self.generate_token(regenerate=True)

		# prepare welcome e-mail
		sender = config.get('mail.noreply')
		message = MIMEMultipart("alternative")
		message["From"] = sender
		message["To"] = username

		# the actual e-mail...
		url_base = config.get("flask.server_name")
		protocol = "https" if config.get("flask.https") else "http"
		url = "%s://%s/reset-password/?token=%s" % (protocol, url_base, register_token)

		# we use slightly different e-mails depending on whether this is the first time setting a password
		if new:
			
			message["Subject"] = "Account created"
			mail = """
			<p>Hello %s,</p>
			<p>A 4CAT account has been created for you. You can now log in to 4CAT at <a href="%s://%s">%s</a>.</p>
			<p>Note that before you log in, you will need to set a password. You can do so via the following link:</p>
			<p><a href="%s">%s</a></p> 
			<p>Please complete your registration within 72 hours as the link above will become invalid after this time.</p>
			""" % (username, protocol, url_base, url_base, url, url)
		else:
			
			message["Subject"] = "Password reset"
			mail = """
			<p>Hello %s,</p>
			<p>Someone has requested a password reset for your 4CAT account. If that someone is you, great! If not, feel free to ignore this e-mail.</p>
			<p>You can change your password via the following link:</p>
			<p><a href="%s">%s</a></p> 
			<p>Please do this within 72 hours as the link above will become invalid after this time.</p>
			""" % (username, url, url)

		# provide a plain-text alternative as well
		html_parser = html2text.HTML2Text()
		message.attach(MIMEText(html_parser.handle(mail), "plain"))
		message.attach(MIMEText(mail, "html"))

		# try to send it
		try:
			send_email([username], message)
			return url
		except (smtplib.SMTPException, ConnectionRefusedError, socket.timeout) as e:
			raise RuntimeError("Could not send password reset e-mail: %s" % e)

	def generate_token(self, username=None, regenerate=True):
		"""
		Generate and store a new registration token for this user

		Tokens are not re-generated if they exist already

		:param username:  Username to generate for: if left empty, it will be
		inferred from self.data
		:param regenerate:  Force regenerating even if token exists
		:return str:  The token
		"""
		if self.data.get("register_token", None) and not regenerate:
			return self.data["register_token"]

		if not username:
			username = self.data["name"]

		register_token = hashlib.sha256()
		register_token.update(os.urandom(128))
		register_token = register_token.hexdigest()
		self.db.update("users", data={"register_token": register_token, "timestamp_token": int(time.time())},
				  where={"name": username})

		return register_token

	def get_value(self, key, default=None):
		"""
		Get persistently stored user property

		:param key:  Name of item to get
		:param default:  What to return if key is not avaiable (default None)
		:return:
		"""
		return self.userdata.get(key, default)

	def set_value(self, key, value):
		"""
		Set persistently stored user property

		:param key:  Name of item to store
		:param value:  Value
		:return:
		"""
		self.userdata[key] = value

		self.db.update("users", where={"name": self.get_id()}, data={"userdata": json.dumps(self.userdata)})

	def set_password(self, password):
		"""
		Set user password

		:param password:  Password to set
		"""
		if self.is_anonymous:
			raise Exception("Cannot set password for anonymous user")

		salt = bcrypt.gensalt()
		password_hash = bcrypt.hashpw(password.encode("ascii"), salt)

		self.db.update("users", where={"name": self.data["name"]}, data={"password": password_hash.decode("utf-8")})
