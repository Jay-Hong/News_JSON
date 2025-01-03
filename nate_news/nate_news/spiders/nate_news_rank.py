import scrapy
from selenium import webdriver; import time
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By; import re;
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from nate_news.items import NateNewsItem; import random

class NateNewsRankSpider(scrapy.Spider):
    name = "nate_news_rank"
    allowed_domains = ["m.news.nate.com"]
    start_urls = ["https://m.news.nate.com/rank/list"]
    
    USER_AGENTS = ['Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36 Edg/112.0.1722.64']

    WINDOW_SIZES = ['window-size=1920x1080']

    LANG = ['lang=ko_KR']

    def __init__(self):
        headlessoptions = webdriver.ChromeOptions()
        headlessoptions.add_argument('headless')
        # 혼돈을 피하기 위해 클래스 변수를 엑세스할 때는 클래스명을 사용하는 것이 좋다. 
        headlessoptions.add_argument(random.choice(NateNewsRankSpider.LANG))
        headlessoptions.add_argument(random.choice(NateNewsRankSpider.WINDOW_SIZES))
        headlessoptions.add_argument(f"User-Agent: {random.choice(NateNewsRankSpider.USER_AGENTS)}")
        self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=headlessoptions)

    def parse(self, response):
        self.driver.get(response.url)
        time.sleep(random.randint(1,3))

        newsURL_list = list()
        newsURL_set = set()

        # 리스트 화면
        links_selector = "#contents > div.rank_news > ol > li > a"
        titles_selector = "#contents > div.rank_news > ol > li > a > h2.context"
        # 뉴스상세 화면
        title_selector = "#artcTitle"
        company_selector = "#contents > div.responsive_wrap > section.rwd_left > div > header > div.medium > div > em > b"
        # 뉴스 이미지
        imageURL_selector = "#content_img > a > img"
        videoURL_selector = "div.view_movie > a > img"
        oneCoURL_selector = "#one_content_img > a > img"

        pattern_thumb = re.compile('thumbnews.nateimg.co.kr/view610///')
        pattern_square_bracket  = re.compile('\[[ㄱ-ㅣ가-힣A-Za-z0-9\t\n\r\f\v]+\]\s*')
        image = ""

        # 시사뉴스 1~50위 가져오기  - - - - - - - - - - - - - - - - - - - - - - -
        self.driver.get("https://m.news.nate.com/rank/list?page=1")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))

        # 시사뉴스 51~100위 가져오기
        self.driver.get("https://m.news.nate.com/rank/list?page=2")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))

        # 스포츠 1~50위 가져오기
        self.driver.get("https://m.news.nate.com/rank/list?section=spo")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))

        # 스포츠 51~100위 가져오기
        self.driver.get("https://m.news.nate.com/rank/list?section=spo&page=2")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))

        # 연예 1~50위 가져오기
        self.driver.get("https://m.news.nate.com/rank/list?section=ent")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))
            
        # 연예 51~100위 가져오기
        self.driver.get("https://m.news.nate.com/rank/list?section=ent&page=2")
        links = self.driver.find_elements(By.CSS_SELECTOR, links_selector)
        titles = self.driver.find_elements(By.CSS_SELECTOR, titles_selector)

        for title_sel, link in zip(titles, links):
            #print(link.get_attribute("href"))
            newsURL_list.append(link.get_attribute("href"))

        # 중복 기사 URL 없애기
        newsURL_set = newsURL_list
        newsURL_list = newsURL_set
        
        for index, newsURL in enumerate(newsURL_list):
            # time.sleep(random.randint(0,1))
            title = ""
            company = ""
            image = ""
            try: 
                self.driver.get(newsURL)
                title_sel = self.driver.find_element(By.CSS_SELECTOR, title_selector)
                company_sel = self.driver.find_element(By.CSS_SELECTOR, company_selector)
            except Exception as e:
                newsURL = ""
                print("기사없음")
            else:
                title = re.sub(pattern_square_bracket, '' , title_sel.text)
                company = company_sel.text
                print(f"{index+1}. {title} | {company}")
            
            try:
                imageURL = self.driver.find_element(By.CSS_SELECTOR, imageURL_selector)        
            except Exception as e:
                print("image 못찾음")
                try:
                    videoURL = self.driver.find_element(By.CSS_SELECTOR, videoURL_selector)
                except Exception as e:
                    print("video 도 못찾음")
                    try:
                        oneCoURL = self.driver.find_element(By.CSS_SELECTOR, oneCoURL_selector)
                    except Exception as e:
                        print("one_content_img 역시 못 찾음")
                        image = ""
                        print(image)
                    else:
                        image = re.sub(pattern_thumb, '' , oneCoURL.get_attribute('src'))
                        print(image)
                else:
                    image = re.sub(pattern_thumb, '' , videoURL.get_attribute('src'))
                    print(image)
            else:
                image = re.sub(pattern_thumb, '' , imageURL.get_attribute('src'))
                print(image)
                
            print(f"{newsURL}")
            
            news_item = NateNewsItem()
            news_item['title'] = title
            news_item['company'] = company
            news_item['image'] = image
            news_item['newsURL'] = newsURL
            yield news_item

        print(f"\n # # # # # # # # # # # # # # # # # # # # # #   정상종료   # # # # # # # # # # # # # # # # # # # # # #\n")
        print(f"\n가져온 기사 수 : {len(newsURL_list)}\n")
        print(f"\n저장된 기사 수 : {len(newsURL_list)}\n")
        self.driver.quit()
        pass
