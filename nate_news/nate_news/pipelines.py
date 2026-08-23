# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem


class NateNewsPipeline:
    def process_item(self, item):
        if len(item["title"]) == 0:  # title 없을 시
            raise DropItem("Drop detail : 제목 없음")
        if len(item["newsURL"]) == 0:  # newsURL 정보 없을 시
            raise DropItem("Drop detail : URL 없음")
        return item


# 중복제거
class DuplicatesPipeline:
    def __init__(self):
        self.titles_seen = set()

    def process_item(self, item):
        adapter = ItemAdapter(item)
        if adapter["title"] in self.titles_seen:
            raise DropItem(f"Duplicate item found: {item!r}")
        self.titles_seen.add(adapter["title"])
        return item
