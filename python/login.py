from selenium import webdriver
from PIL import Image,ImageEnhance
from aip import AipOcr
import time
import yaml
def f():
    with open('cfg.yaml') as f:
        cfg = f.read()
    cfg = yaml.load(cfg)
    browser = webdriver.Chrome()
    browser.implicitly_wait(30)
    browser.get('https://login.1234567.com.cn/login')
    time.sleep(1)
    browser.find_element_by_id('tbname').send_keys(cfg['name'])
    time.sleep(1)
    browser.find_element_by_id('tbpwd').send_keys(cfg['pwd'])
    time.sleep(1)
    screenImg = "screenImg.png"
    browser.get_screenshot_as_file(screenImg)
    img_code = browser.find_element_by_xpath('//*[@id="yzm_top"]/img')
    location = img_code.location
    size = img_code.size

    img = Image.open(screenImg).convert('L').crop(
        (location['x'], location['y'], location['x'] + size['width'], location['y'] + size['height']))
    img.save('code.jpg')
    APP_ID = cfg['APP_ID']
    API_KEY = cfg['API_KEY']
    SECRET_KEY = cfg['SECRET_KEY']

    client = AipOcr(APP_ID, API_KEY, SECRET_KEY)

    def get_file_content(filePath):
        with open(filePath, 'rb') as fp:
            return fp.read()

    image = get_file_content('code.jpg')
    res = client.basicAccurate(image)
    words = res['words_result'][0]['words']
    print(words)
    time.sleep(1)
    browser.find_element_by_id('tbcode').send_keys(words)
    time.sleep(1)
    browser.find_element_by_xpath('//*[@id="winContain"]/div/table/tbody/tr[6]/td/a[1]').click()
    time.sleep(5)
    try:
        browser.find_element_by_id('errmsg').text
    except:
        print('login success')
        # browser.close()
    else:
        print('login falid')
        # browser.close()
        f()
f()
