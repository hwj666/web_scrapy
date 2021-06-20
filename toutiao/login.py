import time
import numpy as np
import math
import cv2
import json
import requests
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

class Cookies:
    def __init__(self,driver):
        self.driver=driver
 
    #获取cookies保存到文件
    def save_cookie(self):
        cookies=self.driver.get_cookies()
        json_cookies=json.dumps(cookies)
        with open('cookies.json','w') as f:
            f.write(json_cookies)
    #读取文件中的cookie
    def add_cookie(self):
        self.driver.delete_all_cookies()
        with open('cookies.json','r',encoding='utf-8') as f:
            cookies=json.load(f)
        for cookie in cookies:
            if 'expiry' in cookie:
                del cookie['expiry']
            self.driver.add_cookie(cookie)


class Toutiao:
    def __init__(self):
        chrome_options = webdriver.ChromeOptions()
        #chrome_options.add_argument("--start-maximized")
        chrome_options.add_experimental_option("excludeSwitches", ['enable-automation',"load-extension"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        # chrome_options.add_experimental_option('debuggerAddress','localhost:9222')
        
        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.delete_all_cookies()
        #self.driver.execute_script('Object.defineProperties(navigator, {webdriver:{get:()=>undefined}});')
        self.driverwait = WebDriverWait(self.driver, 60)
        self.base_url = 'https://mp.toutiao.com/auth/page/login'
        self.cookies = Cookies(self.driver)

    def until_located(self, webType, path):
        element = self.driverwait.until(EC.presence_of_element_located((webType,path)))
        return element
  
    def get_tracks(self, distance, seconds):

        def ease_out_quad(x):
            return 1 - (1 - x) * (1 - x)
        
        def ease_out_quart(x):
            return 1 - pow(1 - x, 4)
        
        def ease_out_expo(x):
            if x == 1:
                return 1
            else:
                return 1 - pow(2, -10 * x)

        tracks = [0]
        offsets = [0]
        for t in np.arange(0.0, seconds, 0.1):
            offset = round(ease_out_expo(t/seconds) * distance)
            tracks.append(offset - offsets[-1])
            offsets.append(offset)
        return offsets, tracks

    def drag_and_drop(self, slide_button, offset, seconds = 2):
  
        offsets, tracks = self.get_tracks(offset, seconds)
        ActionChains(self.driver).click_and_hold(slide_button).perform()
        for x in tracks:
            ActionChains(self.driver).move_by_offset(x, 0).perform()
        ActionChains(self.driver).pause(0.5).release().perform()

    def save_png(self,url, name):
        r = requests.get(url)
        with open(name + '.png', 'wb') as f:
            f.write(r.content)
    
    def FindPic(self):
        target = cv2.imread('target.png', 0)
        template = cv2.imread('template.png',0)
        w,h = template.shape
        result = cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED)
        x, y = np.unravel_index(result.argmax(), result.shape)
        return x,y

    def go_login(self):
        self.driver.get(self.base_url)
        self.until_located(By.CLASS_NAME,'go_login').click()
        # self.until_located(By.ID,'login-type-account').click()

        self.until_located(By.ID,'user-mobile').send_keys('***********')
        self.until_located(By.ID,'mobile-code-get').click()
        # self.until_located(By.ID,'bytedance-login-submit').click()
        
        time.sleep(2)
        captcha_target = self.until_located(By.XPATH,'//*[@id="captcha_container"]/div/div[2]/img[1]')
        captcha_template = self.until_located(By.XPATH,'//*[@id="captcha_container"]/div/div[2]/img[2]')
 
        target_url = captcha_target.get_attribute("src")
        template_url = captcha_template.get_attribute("src")
        self.save_png(target_url,'target')
        self.save_png(template_url,'template')

        slide_button = self.until_located(By.XPATH,'//*[@id="captcha_container"]/div/div[3]/div[2]/div')
        x,y = self.FindPic()
        self.drag_and_drop(slide_button,y)
        self.until_located(By.CLASS_NAME,'new_user_info')
        self.cookies.save_cookie()

    def cook_start(self):
        self.driver.get("https://mp.toutiao.com/profile_v3/xigua/upload-video?from=home")
        self.cookies.add_cookie()
        self.driver.get("https://mp.toutiao.com/profile_v3/xigua/upload-video?from=home")

    def upload_movie(self):
        self.driver.get("https://mp.toutiao.com/profile_v3/xigua/upload-video?from=home")
        self.cookies.add_cookie()
        self.driver.get("https://mp.toutiao.com/profile_v3/xigua/upload-video?from=home")

        # self.until_located(By.XPATH,'//*[@id="root"]/div[2]/div[2]/ul/li[2]/div/span').click()
        # self.until_located(By.XPATH,'//*[@id="root"]/div[2]/div[2]/ul/li[2]/ul/li[2]/a').click()
        time.sleep(5)
        self.until_located(By.XPATH,'//*[@id="xigua"]/div/div[1]/div[1]/div[1]/div[1]/input').send_keys('/Users/didi/Desktop/奔驰的发明.mp4')

toutiao = Toutiao()
# toutiao.go_login()
toutiao.cook_start()
# toutiao.upload_movie()