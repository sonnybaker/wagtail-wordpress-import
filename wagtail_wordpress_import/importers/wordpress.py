import json
from datetime import datetime
from functools import cached_property
from xml.dom import pulldom

from django.apps import apps
from django.utils.text import slugify
from django.utils.timezone import make_aware
from wagtail.core.models import Page
from wagtail_wordpress_import.bleach import bleach_clean, fix_styles
from wagtail_wordpress_import.block_builder import BlockBuilder
from wagtail_wordpress_import.functions import linebreaks_wp, node_to_dict


class WordpressImporter:
    def __init__(self, xml_file_path):
        self.xml_file = xml_file_path
        self.log_processed = 0
        self.log_imported = 0
        self.log_skipped = 0
        self.logged_items = []

    def run(self, *args, **kwargs):
        xml_doc = pulldom.parse(self.xml_file)

        try:
            self.page_model_instance = apps.get_model(
                kwargs["app_for_pages"], kwargs["model_for_pages"]
            )
        except LookupError:
            print(
                f"The app `{kwargs['app_for_pages']}` and/or page model `{kwargs['model_for_pages']}` cannot be found!"
            )
            print(
                "Check the command line options -a and -m match an existing Wagtail app and Wagtail page model"
            )
            exit()

        try:
            self.parent_page_obj = Page.objects.get(pk=kwargs["parent_id"])
        except Page.DoesNotExist:
            print(f"A page with id {kwargs['parent_id']} does not exist")
            exit()

        for event, node in xml_doc:
            # each node represents a tag in the xml
            # event is true for the start element
            if event == pulldom.START_ELEMENT and node.tagName == "item":
                xml_doc.expandNode(node)
                item = node_to_dict(node)
                self.log_processed += 1

                if (
                    item.get("wp:post_type") in kwargs["page_types"]
                    and item.get("wp:status") in kwargs["page_statuses"]
                ):

                    wordpress_item = WordpressItem(item)

                    try:
                        page = self.page_model_instance.objects.get(
                            wp_post_id=wordpress_item.cleaned_data.get("wp_post_id")
                        )
                    except self.page_model_instance.DoesNotExist:
                        page = self.page_model_instance()

                    page.import_wordpress_data(wordpress_item.cleaned_data)

                    if item.get("wp:status") == "draft":
                        setattr(page, "live", False)
                    else:
                        setattr(page, "live", True)

                    if page.id:
                        page.save()
                        self.logged_items.append(
                            {
                                "result": "updated",
                                "reason": "existed",
                                "datecheck": wordpress_item.date_changed,
                                "slugcheck": wordpress_item.slug_changed,
                            }
                        )

                    else:
                        self.parent_page_obj.add_child(instance=page)
                        page.save()
                        self.logged_items.append(
                            {
                                "result": "updated",
                                "reason": "existed",
                                "datecheck": wordpress_item.date_changed,
                                "slugcheck": wordpress_item.slug_changed,
                            }
                        )

        return (
            self.log_imported,
            self.log_skipped,
            self.log_processed,
            self.logged_items,
        )

    def analyze_html(self, html_analyzer, *, page_types, page_statuses):
        xml_doc = pulldom.parse(self.xml_file)

        for event, node in xml_doc:
            # each node represents a tag in the xml
            # event is true for the start element
            if event == pulldom.START_ELEMENT and node.tagName == "item":
                xml_doc.expandNode(node)
                item = node_to_dict(node)
                if (
                    item.get("wp:post_type") in page_types
                    and item.get("wp:status") in page_statuses
                ):
                    stream_fields = self.mapping_stream_fields.split(",")

                    for html in stream_fields:
                        value = linebreaks_wp(
                            item.get(self.mapping_item_inverse.get(html))
                        )
                        html_analyzer.analyze(value)


class WordpressItem:
    def __init__(self, node):
        self.node = node
        self.raw_body = self.node["content:encoded"]
        self.linebreaks_wp = linebreaks_wp(self.raw_body)
        self.fix_styles = fix_styles(self.linebreaks_wp)
        self.bleach_clean = bleach_clean(self.fix_styles)
        self.slug_changed = None
        self.date_changed = False

    def cleaned_title(self):
        return self.node["title"].strip()

    def cleaned_slug(self):
        """
        Oddly some page have no slug and some have illegal characters!
        If None make one from title.
        Also pass any slug through slugify to be sure and if it's chnaged make a note
        """

        if not self.node["wp:post_name"]:
            slug = slugify(self.cleaned_title())
            self.slug_changed = "blank slug"  # logging
        else:
            slug = slugify(self.node["wp:post_name"])

        # some slugs have illegal characters so it will be changed
        if slug != self.node["wp:post_name"]:
            self.slug_changed = "slug fixed"  # logging

        return slug

    def cleaned_first_published_at(self):
        return self.clean_date(self.node["wp:post_date_gmt"].strip())

    def cleaned_last_published_at(self):
        return self.clean_date(self.node["wp:post_modified_gmt"].strip())

    def cleaned_latest_revision_created_at(self):
        return self.clean_date(self.node["wp:post_modified_gmt"].strip())

    def clean_date(self, value):
        """
        We need a nice date to be able to save the page later. Some dates are not suitable
        date strings in the xml. If thats the case return a specific date so it can be saved
        and return the failure for logging
        """

        if value == "0000-00-00 00:00:00":
            # setting this date so it can be found in wagtail admin
            value = "1900-01-01 00:00:00"
            self.date_changed = True

        date_utc = "T".join(value.split(" "))
        date_formatted = make_aware(datetime.strptime(date_utc, "%Y-%m-%dT%H:%M:%S"))

        return date_formatted

    def cleaned_raw_content(self):
        # no cleaning here
        return self.node["content:encoded"]

    def cleaned_post_id(self):
        return int(self.node["wp:post_id"])

    def cleaned_post_type(self):
        return str(self.node["wp:post_type"].strip())

    def cleaned_link(self):
        return str(self.node["link"].strip())

    def cleaned_body(self):
        return json.dumps(BlockBuilder(self.bleach_clean).build())

    @cached_property
    def cleaned_data(self):
        return {
            "title": self.cleaned_title(),
            "slug": self.cleaned_slug(),
            "first_published_at": self.cleaned_first_published_at(),
            "last_published_at": self.cleaned_last_published_at(),
            "latest_revision_created_at": self.cleaned_latest_revision_created_at(),
            "body": self.cleaned_body(),
            "wp_post_id": self.cleaned_post_id(),
            "wp_post_type": self.cleaned_post_type(),
            "wp_link": self.cleaned_link(),
            "wp_raw_content": self.raw_body,
            "wp_processed_content": self.fix_styles,
            "wp_block_json": self.cleaned_body(),
        }
