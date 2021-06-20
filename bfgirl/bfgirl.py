# -*- coding: utf-8 -*-

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoAlertPresentException,TimeoutException,NoSuchElementException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By



import requests
import time
import random

def saveImg(url,path,name):
    getHeaders = {
        'Connection': 'Keep-Alive',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.132 Safari/537.36',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9'
    }
    getHeaders['Referer'] = 'http://www.mmjpg.com/'
    response = requests.get(url, headers=getHeaders)
    if (response.status_code == 404):  # 若404错误，递归get，尝试非重定向方式获取
        response = requests.get(url, headers=getHeaders, allow_redirects=False)
        if (response.status_code == 302):  # 302表示访问对象已被移动到新位置，但仍按照原地址进行访问（造成404错误）。
            redirectUrl = response.headers['location']  # 因此需在响应头文件中获取重定向后地址
            response = requests.get(redirectUrl)
            fp = open("{}/{}.jpg".format(path, name), 'ab')
            fp.write(response.content)
            fp.close()
    else:
        fp = open("{}/{}.jpg".format(path,name), 'ab')
        fp.write(response.content)
        fp.close()

    t = random.randint(5,8)
    time.sleep(t)


chrome_options = Options()
chrome_options.add_argument('--headless')
driver = webdriver.Chrome(options=chrome_options)



start_url = 'http://www.mmjpg.com/mm/1532'
urls = []

try:
    driver.get(start_url)
    driver.maximize_window()
    driver.implicitly_wait(8)
    i = 0
    while True:
        
        
        element = WebDriverWait(driver,10,1).until(
                EC.presence_of_element_located((By.XPATH,'//*[@id="content"]/a/img')))
        img_url = element.get_attribute('src')
        saveImg(img_url,'imgs',i)
        i += 1
        try:
            driver.find_element_by_link_text('下一张').click()
        except:
            driver.find_element_by_link_text('下一篇').click()

except Exception as e:
    print(e)
finally:
    driver.close()
    

