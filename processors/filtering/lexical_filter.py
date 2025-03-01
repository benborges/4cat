"""
Filter posts by lexicon
"""
import re
import csv
from pathlib import Path

from backend.abstract.processor import BasicProcessor
from common.lib.helpers import UserInput

import common.config_manager as config
__author__ = "Stijn Peeters"
__credits__ = ["Stijn Peeters"]
__maintainer__ = "Stijn Peeters"
__email__ = "4cat@oilab.eu"

csv.field_size_limit(1024 * 1024 * 1024)


class LexicalFilter(BasicProcessor):
	"""
	Retain only posts matching a given lexicon
	"""
	type = "lexical-filter"  # job type ID
	category = "Filtering"  # category
	title = "Filter by words or phrases"  # title displayed in UI
	description = "Retains posts that contain selected words or phrases, including preset word lists. " \
				  "This creates a new dataset."  # description displayed in UI
	extension = "csv"  # extension of result file, used internally and in UI

	references = [
		"[Hatebase](https://hatebase.org)",
		"[Regex101](https://regex101.com/)"
	]

	# the following determines the options available to the user via the 4CAT
	# interface.
	options = {
		"lexicon": {
			"type": UserInput.OPTION_MULTI,
			"default": [],
			"options": {
				"hatebase-en-unambiguous": "Hatebase.org hate speech list (English, unambiguous terms)",
				"hatebase-en-ambiguous": "Hatebase.org hate speech list (English, ambiguous terms)",
			},
			"help": "Filter items containing words in these lexicons. Note that they may be outdated."
		},
		"lexicon-custom": {
			"type": UserInput.OPTION_TEXT,
			"default": "",
			"help": "Custom word list (separate with commas)"
		},
		"as_regex": {
			"type": UserInput.OPTION_TOGGLE,
			"default": False,
			"help": "Interpret custom word list as a regular expression",
			"tooltip": "Regular expressions are parsed with Python"
		},
		"exclude": {
			"type": UserInput.OPTION_TOGGLE,
			"default": False,
			"help": "Should not include the above word(s)",
			"tooltip": "Only posts that do not match the above words are retained"
		},
		"case-sensitive": {
			"type": UserInput.OPTION_TOGGLE,
			"default": False,
			"help": "Case sensitive"
		}
	}

	def process(self):
		"""
		Reads a CSV file, counts occurrences of chosen values over all posts,
		and aggregates the results per chosen time frame
		"""

		exclude = self.parameters.get("exclude", False)
		case_sensitive = self.parameters.get("case-sensitive", False)

		# load lexicons from word lists
		lexicons = {}
		for lexicon_id in self.parameters.get("lexicon", []):
			lexicon_file = Path(config.get('PATH_ROOT'), "common/assets/wordlists/%s.txt" % lexicon_id)
			if not lexicon_file.exists():
				continue

			if lexicon_id not in lexicons:
				lexicons[lexicon_id] = set()

			with open(lexicon_file, encoding="utf-8") as lexicon_handle:
				lexicons[lexicon_id] |= set(lexicon_handle.read().splitlines())

		# add user-defined words
		custom_id = "user-defined"
		if custom_id not in lexicons:
			lexicons[custom_id] = set()

		custom_lexicon = set(
			[word.strip() for word in self.parameters.get("lexicon-custom", "").split(",") if word.strip()])
		lexicons[custom_id] |= custom_lexicon

		# compile into regex for quick matching
		lexicon_regexes = {}
		for lexicon_id in lexicons:
			if not lexicons[lexicon_id]:
				continue

			if not self.parameters.get("as_regex"):
				phrases = [re.escape(term) for term in lexicons[lexicon_id] if term]
			else:
				phrases = [term for term in lexicons[lexicon_id] if term]

			try:
				if not case_sensitive:
					lexicon_regexes[lexicon_id] = re.compile(
					r"\b(" + "|".join(phrases) + r")\b",
					flags=re.IGNORECASE)
				else:
					lexicon_regexes[lexicon_id] = re.compile(
					r"\b(" + "|".join(phrases) + r")\b")
			except re.error:
				self.dataset.update_status("Invalid regular expression, cannot use as filter", is_final=True)
				self.dataset.finish(0)
				return

		# now for the real deal
		self.dataset.update_status("Reading source file")

		# keep some stats
		processed = 0
		matching_items = 0

		with self.dataset.get_results_path().open("w", encoding="utf-8", newline="") as output:
			# get header row, we need to copy it for the output
			fieldnames = self.source_dataset.get_item_keys(self)

			# start the output file
			fieldnames.append("matching_lexicons")
			writer = csv.DictWriter(output, fieldnames=fieldnames)
			writer.writeheader()

			# iterate through posts and see if they match
			for post in self.source_dataset.iterate_items(self):
				if not post.get("body", None):
					continue

				if processed % 2500 == 0:
					self.dataset.update_status("Processed %i posts (%i matching)" % (processed, matching_items))
					self.dataset.update_progress(processed / self.source_dataset.num_rows)

				# if 'partition' is false, there will just be one combined
				# lexicon, but else we'll have different ones we can
				# check separately
				matching_lexicons = set()
				for lexicon_id in lexicons:
					if lexicon_id not in lexicon_regexes:
						continue

					lexicon_regex = lexicon_regexes[lexicon_id]

					# check if we match
					if not lexicon_regex.findall(post["body"]) and not exclude:
						continue
					elif lexicon_regex.findall(post["body"]) and exclude:
						continue

					matching_lexicons.add(lexicon_id)

				# if none of the lexicons match, the post is not retained
				processed += 1
				if not matching_lexicons:
					continue

				# if one does, record which match, and save it to the output
				post["matching_lexicons"] = ",".join(matching_lexicons)
				writer.writerow(post)

				matching_items += 1

		if matching_items > 0:
			self.dataset.update_status("New dataset created with %i matching post(s)" % matching_items, is_final=True)
		self.dataset.finish(matching_items)

	def after_process(self):
		super().after_process()

		# Request standalone
		self.create_standalone()
