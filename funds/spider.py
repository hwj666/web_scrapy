# coding=utf-8
#%%
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

start_url = 'http://fund.eastmoney.com/allfund.html'

chrome_options = Options()
chrome_options.add_argument('--headless')
driver = webdriver.Chrome(chrome_options=chrome_options)
driver.get(start_url)
# funds = driver.find_elements_by_xpath('//*[@id="code_content"]/div[1]/ul/li/div/a[1]')

funds2 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[2]/ul/li/div/a[1]')
funds3 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[3]/ul/li/div/a[1]')
funds4 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[4]/ul/li/div/a[1]')
funds5 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[5]/ul/li/div/a[1]')
funds6 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[6]/ul/li/div/a[1]')
funds7 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[7]/ul/li/div/a[1]')
funds8 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[8]/ul/li/div/a[1]')
funds9 = driver.find_elements_by_xpath('//*[@id="code_content"]/div[9]/ul/li/div/a[1]')

with open('funds1.txt','w') as f:
    for funds in [funds2,funds3,funds4,funds5,funds6,funds7,funds8,funds9]:
        for fund in funds:
            f.write(fund.text + '\n')

driver.close()
