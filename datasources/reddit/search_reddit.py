import requests
import json
import time
import re

from backend.abstract.search import SearchWithScope
from common.lib.exceptions import QueryParametersException, ProcessorInterruptedException
from common.lib.helpers import UserInput


class SearchReddit(SearchWithScope):
	"""
	Search Reddit

	Defines methods to fetch Reddit data on demand
	"""
	type = "reddit-search"  # job ID
	category = "Search"  # category
	title = "Reddit Search"  # title displayed in UI
	description = "Query the Pushshift API to retrieve Reddit posts and threads matching the search parameters"  # description displayed in UI
	extension = "csv"  # extension of result file, used internally and in UI
	is_local = False	# Whether this datasource is locally scraped
	is_static = False	# Whether this datasource is still updated
	
	references = [
		"[API documentation](https://github.com/pushshift/api)",
		"[r/pushshift](https://www.reddit.com/r/pushshift/)",
		"[Baumgartner, J., Zannettou, S., Keegan, B., Squire, M., & Blackburn, J. (2020). The Pushshift Reddit Dataset. *Proceedings of the International AAAI Conference on Web and Social Media*, 14(1), 830-839.](https://ojs.aaai.org/index.php/ICWSM/article/view/7347)"
	]

	# not available as a processor for existing datasets
	accepts = [None]

	max_workers = 1
	max_retries = 5

	rate_limit = 0
	request_timestamps = []

	options = {
		"wildcard-warning": {
			"type": UserInput.OPTION_INFO,
			"help": "The requirement for searching by keyword has been lifted for your account; you can search by "
					"date range only. This can potentially return hundreds of millions of posts, so **please be "
					"careful** when using this privilege.",
			"requires": "reddit.can_query_without_keyword"
		},
		"pushshift_track": {
			"type": UserInput.OPTION_CHOICE,
			"help": "API version",
			"options": {
				"regular": "Regular",
				"beta": "Beta (experimental)"
			},
			"default": "regular",
			"tooltip": "The beta version retrieves more comments per request but may be incomplete."
		},
		"board": {
			"type": UserInput.OPTION_TEXT,
			"help": "Subreddit(s)",
			"tooltip": "Comma-separated"
		},
		"divider": {
			"type": UserInput.OPTION_DIVIDER
		},
		"intro": {
			"type": UserInput.OPTION_INFO,
			"help": "Reddit data is retrieved from [Pushshift](https://pushshift.io) (see also [this "
					"paper](https://ojs.aaai.org/index.php/ICWSM/article/view/7347)). Note that Pushshift's dataset "
					"*may not be complete* depending on the parameters used,"
					" data from the last few days might not be there yet,"
					" and post scores can be out of date. "
					"See [this paper](https://arxiv.org/pdf/1803.05046.pdf) for an overview of the gaps in data. "
					"Double-check manually or via the official Reddit API if completeness is a concern. Check the "
					"[Pushshift API](https://github.com/pushshift/api) reference [beta](https://beta.pushshift.io/redoc) for "
					"documentation on query syntax, "
					"e.g. on how to format keyword queries."
		},
		"body_match": {
			"type": UserInput.OPTION_TEXT,
			"help": "Message search",
			"tooltip": "Matches anything in the body of a comment or post."
		},
		"subject_match": {
			"type": UserInput.OPTION_TEXT,
			"help": "Subject search",
			"tooltip": "Matches anything in the title of a post."
		},
		"subject_url": {
			"type": UserInput.OPTION_TEXT,
			"help": "URL/domain in post"
		},
		"divider-2": {
			"type": UserInput.OPTION_DIVIDER
		},
		"daterange": {
			"type": UserInput.OPTION_DATERANGE,
			"help": "Date range"
		},
		"search_scope": {
			"type": UserInput.OPTION_CHOICE,
			"help": "Search scope",
			"options": {
				"op-only": "Opening posts only (no replies/comments)",
				"posts-only": "All matching posts",
#				"full-threads": "All posts in threads with matching posts (full threads)",
#				"dense-threads": "All posts in threads in which at least x% of posts match (dense threads)"
			},
			"default": "posts-only"
		},
		"scope_density": {
			"type": UserInput.OPTION_TEXT,
			"help": "Min. density %",
			"min": 0,
			"max": 100,
			"default": 15,
			"tooltip": "At least this many % of posts in the thread must match the query"
		},
		"scope_length": {
			"type": UserInput.OPTION_TEXT,
			"help": "Min. dense thread length",
			"min": 30,
			"default": 30,
			"tooltip": "A thread must at least be this many posts long to qualify as a 'dense thread'"
		}
	}

	# These change depending on the API type used,
	# but should be globally accessible.
	submission_endpoint = None
	comment_endpoint = None
	api_type = None
	since = "since"
	after = "after"

	def get_items_simple(self, query):
		"""
		In the case of Reddit, there is no need for multiple pathways, so we
		can route it all to the one post query method.
		:param query:
		:return:
		"""
		return self.get_items_complex(query)

	def get_items_complex(self, query):
		"""
		Execute a query; get post data for given parameters

		This queries the Pushshift API to find posts and threads mathcing the
		given parameters.

		:param dict query:  Query parameters, as part of the DataSet object
		:return list:  Posts, sorted by thread and post ID, in ascending order
		"""
		scope = query.get("search_scope")
		self.api_type = query.get("pushshift_track", "regular")
		
		# first, build the request parameters
		if self.api_type == "regular":
			self.submission_endpoint = "https://api.pushshift.io/reddit/submission/search"
			self.comment_endpoint = "https://api.pushshift.io/reddit/comment/search"
			
			post_parameters = {
				"sort": "asc",
				"sort_type": "created_utc",
				"size": 100, # max value
				"metadata": True
			}
			since = "after"
			until = "before"

		# beta fields are a bit different.
		elif self.api_type == "beta":
			self.submission_endpoint = "https://beta.pushshift.io/reddit/search/submissions"
			self.comment_endpoint = "https://beta.pushshift.io/reddit/search/comments"

			# For beta requests, we're sorting by IDs so we're not missing data.
			# This is unavailable for the regular API.
			post_parameters = {
				"sort": "id",
				"order": "asc",
				"limit": 1000 # max value
			}
			since = "since"
			until = "until"

		if query["min_date"]:
			post_parameters[since] = int(query["min_date"])

		if query["max_date"]:
			post_parameters[until] = int(query["max_date"])

		if query["board"] and query["board"] != "*":
			post_parameters["subreddit"] = query["board"]

		if query["body_match"]:
			post_parameters["q"] = query["body_match"]
		else:
			post_parameters["q"] = ""

		# set up query
		total_posts = 0
		max_retries = 3

		# get rate limit from API server when using the regular API
		if self.api_type != "regular":
			try:
				api_metadata = requests.get("https://api.pushshift.io/meta").json()
				self.rate_limit = api_metadata["server_ratelimit_per_minute"]
				self.log.info("Got rate limit from Pushshift: %i requests/minute" % self.rate_limit)
			except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
				self.log.warning("Could not retrieve rate limit from Pushshift: %s" % e)
				self.rate_limit = 120
		else:
			self.rate_limit = 120

		# first, search for threads - this is a separate endpoint from comments
		submission_parameters = post_parameters.copy()
		submission_parameters["selftext"] = submission_parameters["q"]

		if query["subject_match"]:
			submission_parameters["title"] = query["subject_match"]

		# Check whether only OPs linking to certain URLs should be retreived.
		# Only available for the regular API.
		if query.get("subject_url", None):
			urls = []
			domains = []

			if "," in query["subject_url"]:
				urls_input = query["subject_url"].split(",")
			elif "|" in query["subject_url"]:
				urls_input = query["subject_url"].split("|")
			else:
				urls_input = [query["subject_url"]]

			# Input strings
			for url in urls_input:

				# Some cleaning
				url = url.strip()

				url_clean = url.replace("http://", "")
				url_clean = url.replace("https://", "")
				url_clean = url.replace("www.", "")

				# Store urls or domains separately; different fields in Pushshift API
				if "/" in url_clean:
					urls.append(url)
				else:
					domains.append(url_clean)
			if urls:
				# Multiple full URLs is supposedly not supported by Pushshift
				submission_parameters["url"] = "\'" + (",".join(urls)) + "\'"
			if domains:
				submission_parameters["domain"] = ",".join(domains)

		# this is where we store our progress
		thread_ids = []
		total_threads = 0
		seen_threads = set()

		# loop through results bit by bit
		while True:
			if self.interrupted:
				raise ProcessorInterruptedException("Interrupted while fetching thread data from the Pushshift API")

			retries = 0
			response = self.call_pushshift_api(self.submission_endpoint,
											   params=submission_parameters)
			
			if response is None:
				return response

			threads = response.json()["data"]

			if len(threads) == 0:
				# we're done here, no more results will be coming
				break

			# store comment IDs for a thread, and also add the OP to the
			# return list. This means all OPs will come before all comments
			# but we can sort later if that turns out to be a problem
			for thread in threads:
				if thread.get("promoted", False):
					continue

				if thread["id"] not in seen_threads:
					seen_threads.add(thread["id"])
					yield self.thread_to_4cat(thread)

					# For the regular API, increase the time.
					# this is the only way to go to the next page right now...
					if self.api_type == "regular":
						submission_parameters[since] = thread["created_utc"]
					# For the beta API, we can sort by IDs and only get those higher than the last encountered.
					elif self.api_type == "beta":
						submission_parameters["min_id"] = thread["id"] + 1

					total_threads += 1

			# update status
			self.dataset.update_status("Retrieved %i threads via Pushshift API." % total_threads)

		# okay, search the pushshift API for posts
		# we have two modes here: by keyword, or by ID. ID is set above where
		# ID chunks are defined: these chunks are used here if available
		seen_posts = set()

		# only query for individual posts if no subject keyword is given
		# since individual posts don't have subjects so if there is a subject
		# query no results should be returned
		do_body_query = not bool(query.get("subject_match", "")) and not bool(query.get("subject_url", "")) and scope != "op-only"

		while do_body_query:
			if self.interrupted:
				raise ProcessorInterruptedException("Interrupted while fetching post data from the Pushshiwft API")

			response = self.call_pushshift_api(self.comment_endpoint, params=post_parameters)

			if response is None:
				return response

			if retries >= max_retries:
				self.log.error("Error during pushshift fetch of query %s" % self.dataset.key)
				self.dataset.update_status("Error while searching for posts on Pushshift")
				return None

			# no more posts
			posts = response.json()["data"]
			
			if len(posts) == 0:
				# this could happen in some edge cases if we're searching by
				# chunk (if no IDs in the chunk match the other parameters)
				# so only break if that's not the case
				break

			# store post data
			for post in posts:
				if post.get("promoted", False):
					continue

				if post["id"] not in seen_posts:
					seen_posts.add(post["id"])
					yield self.post_to_4cat(post)

					# For the regular API, increase the time.
					# this is the only way to go to the next page right now...
					if self.api_type == "regular":
						post_parameters[self.since] = post["created_utc"]
					# For the beta API, we can sort by IDs and only get those higher than the last encountered.
					elif self.api_type == "beta":
						post_parameters["min_id"] = post["id"] + 1

					total_posts += 1

			# update our progress
			self.dataset.update_status("Found %i comments via Pushshift API..." % total_posts)

		# and done!
		if total_posts == 0 and total_threads == 0:
			self.dataset.update_status("No posts found")

	def fetch_posts(self, post_ids, where=None, replacements=None):
		"""
		Fetch post data from Pushshift API by post ID

		:param list post_ids:  List of post IDs to return data for
		:return list: List of posts, with a dictionary representing the record for each post
		"""
		chunk_size = 500
		posts = []

		# search threads in chunks
		offset = 0
		while True:
			if self.interrupted:
				raise ProcessorInterruptedException("Interrupted while fetching posts from the Pushshift API")

			chunk = post_ids[offset:offset + chunk_size]
			if not chunk:
				break

			response = self.call_pushshift_api(self.comment_endpoint + "?ids=" + ",".join(chunk))

			if not response:
				break

			for post in response.json()["data"]:
				posts.append(self.post_to_4cat(post))

			offset += chunk_size

		return posts

	def fetch_threads(self, thread_ids):
		"""
		Get all posts for given thread IDs

		The pushshift API at this time has no endpoint that retrieves comments
		for multiple threads at the same time, so unfortunately we have to go
		through the threads one by one.

		:param tuple thread_ids:  Thread IDs to fetch posts for.
		:return list:  A list of posts, as dictionaries.
		"""
		posts = []
		seen_posts = set()
		expected_results_per_page = 100  # max results per page in API

		# search threads in chunks
		offset = 0
		for thread_id in thread_ids:
			offset += 1
			self.dataset.update_status("Retrieving posts for thread %i of %i" % (offset, len(thread_ids)))

			if self.interrupted:
				raise ProcessorInterruptedException("Interrupted while fetching threads from the Pushshift API")

			thread_params = {"link_id": thread_id, "size": expected_results_per_page, "sort": "asc", "sort_type": "created_utc"}
			while True:
				# can't get all posts in one request, so loop until thread is
				# exhausted
				response = self.call_pushshift_api(self.comment_endpoint,
												   params=thread_params)
				if response is None:
					# error or empty response
					break

				posts_raw = response.json()["data"]
				latest_timestamp = 0
				first_timestamp = time.time()

				for post in posts_raw:
					if post["id"] in seen_posts:
						# pagination by timestamp may lead to duplicate results
						continue

					seen_posts.add(post["id"])
					posts.append(self.post_to_4cat(post))
					latest_timestamp = max(latest_timestamp, post["created_utc"])
					first_timestamp = min(post["created_utc"], first_timestamp)

				if len(posts_raw) < expected_results_per_page:
					# no results beyond this response
					break

				# get all posts after the latest in the set - there is no
				# explicit pagination in Pushshift's API
				# we can only paginate by increasing the 'after timestamp'
				# parameter, but *if* there are 100 posts at the same second
				# which is unlikely but possible, this will fail, so if all
				# posts have the same timestamp, allow a one-second gap
				# this might miss posts but there is no better way with this
				# API since 'after_id' does not work
				if latest_timestamp == first_timestamp:
					latest_timestamp += 1

				thread_params[self.since] = latest_timestamp

		return posts

	def get_thread_sizes(self, thread_ids, min_length):
		"""
		Get thread lengths for all threads

		:param tuple thread_ids:  List of thread IDs to fetch lengths for
		:param int min_length:  Min length for a thread to be included in the
		results
		:return dict:  Threads sizes, with thread IDs as keys
		"""
		chunk_size = 500
		chunks = []
		lengths = {}
		thread_ids = list(set(thread_ids))  # deduplicate

		# search threads in chunks
		offset = 0
		while True:
			chunk = thread_ids[offset:offset + chunk_size]
			if not chunk:
				break

			response = self.call_pushshift_api(self.submission_endpoint + "?ids=" + ",".join(chunk))
			if response is None:
				break

			for thread in response.json()["data"]:
				length = thread["num_comments"]
				if length >= min_length:
					lengths[thread["id"]] = length

			offset += chunk_size

		return lengths

	def post_to_4cat(self, post):
		"""
		Convert a pushshift post object to 4CAT post data

		:param dict post:  Post data, as from the pushshift API
		:return dict:  Re-formatted data
		"""
		
		return {
			"thread_id": post["link_id"].split("_")[1] if self.api_type == "regular" else post["link_id"],
			"id": post["id"],
			"timestamp": post["created_utc"],
			"body": post["body"].strip().replace("\r", ""),
			"subject": "",
			"author": post["author"],
			"author_flair": post.get("author_flair_text", ""),
			"post_flair": "",
			"domain": "",
			"url": "",
			"image_file": "",
			"image_md5": "",
			"subreddit": post["subreddit"],
			"parent": post["parent_id"],
			# this is missing sometimes, but upon manual inspection
			# the post always has 1 point
			"score": post.get("score", 1)
		}

	def thread_to_4cat(self, thread):
		"""
		Convert a pushshift thread object to 4CAT post data

		:param dict post:  Post data, as from the pushshift API
		:return dict:  Re-formatted data
		"""
		image_match = re.compile(r"\.(jpg|jpeg|png|gif|webm|mp4)$", flags=re.IGNORECASE)

		return {
			"thread_id": thread["id"],
			"id": thread["id"],
			"timestamp": thread["created_utc"],
			"body": thread.get("selftext", "").strip().replace("\r", ""),
			"subject": thread["title"],
			"author": thread["author"],
			"author_flair": thread.get("author_flair_text", ""),
			"post_flair": thread.get("link_flair_text", ""),
			"image_file": thread["url"] if image_match.search(thread["url"]) else "",
			"domain": thread["domain"],
			"url": thread["url"],
			"image_md5": "",
			"subreddit": thread["subreddit"],
			"parent": "",
			"score": thread.get("score", 0)
		}

	def call_pushshift_api(self, *args, **kwargs):
		"""
		Call pushshift API and don't crash (immediately) if it fails

		Will also try to respect the rate limit, waiting before making a
		request until it will not violet the rate limit.

		:param args:
		:param kwargs:
		:return: Response, or `None`
		"""

		retries = 0
		while retries < self.max_retries:
			try:
				self.wait_until_window()
				response = requests.get(*args, **kwargs)
				self.request_timestamps.append(time.time())
				if response.status_code == 200:
					break
				else:
					raise RuntimeError("HTTP %s" % response.status_code)
			except (RuntimeError, requests.RequestException) as e:
				self.log.info("Error %s while querying Pushshift API - waiting 15 seconds and retrying..." % e)
				time.sleep(15)
				retries += 1

		if retries >= self.max_retries:
			self.log.error("Error during Pushshift fetch of query %s" % self.dataset.key)
			self.dataset.update_status("Error while searching for posts on Pushshift - API did not respond as expected")
			return None

		return response

	def wait_until_window(self):
		"""
		Wait until a request can be made outside of the rate limit

		If we have made more requests in the window (one minute) than allowed
		by the rate limit, wait until that is no longer the case.
		"""
		window_start = time.time() - 60
		has_warned = False
		while len([timestamp for timestamp in self.request_timestamps if timestamp >= window_start]) >= self.rate_limit:
			if not has_warned:
				self.log.info("Hit Pushshift rate limit - throttling...")
				has_warned = True

			time.sleep(0.25) # should be enough

		# clean up timestamps outside of window
		self.request_timestamps = [timestamp for timestamp in self.request_timestamps if timestamp >= window_start]

	def validate_query(query, request, user):
		"""
		Validate input for a dataset query on the 4chan data source.

		Will raise a QueryParametersException if invalid parameters are
		encountered. Mutually exclusive parameters may also be sanitised by
		ignoring either of the mutually exclusive options.

		:param dict query:  Query parameters, from client-side.
		:param request:  Flask request
		:param User user:  User object of user who has submitted the query
		:return dict:  Safe query parameters
		"""
		# we need a board!
		r_prefix = re.compile(r"^/?r/")
		boards = [r_prefix.sub("", board).strip() for board in query.get("board", "").split(",") if board.strip()]

		if not boards:
			raise QueryParametersException("Please provide a board or a comma-separated list of boards to query.")

		# ignore leading r/ for boards
		query["board"] = ",".join(boards)
		
		# this is the bare minimum, else we can't narrow down the full data set
		if not user.is_admin and not user.get_value("reddit.can_query_without_keyword", False) and not query.get("body_match", "").strip() and not query.get("subject_match", "").strip() and not query.get("subject_url", ""):
			raise QueryParametersException("Please provide a body query or subject query.")

		# body query and full threads are incompatible, returning too many posts
		# in most cases
		if query.get("body_match", None):
			if "full_threads" in query:
				del query["full_threads"]

		# Make sure no body or subject searches starting with just a minus sign are possible, e.g. "-Trump"
		if query.get("body_match", None) or query.get("subject_match", None):
			queries_to_check = []

			if query.get("body_match", None):
				queries_to_check += [body_query.strip() for body_query in query["body_match"].split(" ")]

			if query.get("subject_match", None):
				queries_to_check += [subject_query.strip() for subject_query in query["subject_match"].split(" ")]

			startswith_minus = [query_check.startswith("-") for query_check in queries_to_check]
			if all(startswith_minus):
				raise QueryParametersException("Please provide body queries that do not start with a minus sign.")

		# URL queries are not possible (yet) for the beta API
		if query.get("pushshift_track") == "beta" and query.get("subject_url", None):
			raise QueryParametersException("URL querying is not possible (yet) for the beta endpoint.")

		# both dates need to be set, or none
		if query.get("min_date", None) and not query.get("max_date", None):
			raise QueryParametersException("When setting a date range, please provide both an upper and lower limit.")

		# the dates need to make sense as a range to search within
		query["min_date"], query["max_date"] = query.get("daterange")
		
		if "*" in query.get("body_match", "") and not user.get_value("reddit.can_query_without_keyword", False):
			raise QueryParametersException("Wildcard queries are not allowed as they typically return too many results to properly process.")

		if "*" in query.get("board", "") and not user.get_value("reddit.can_query_without_keyword"):
			raise QueryParametersException("Wildcards are not allowed for boards as this typically returns too many results to properly process.")

		del query["daterange"]
		if query.get("search_scope") not in ("dense-threads",):
			del query["scope_density"]
			del query["scope_length"]

		# if we made it this far, the query can be executed
		return query

