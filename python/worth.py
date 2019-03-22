# -*- coding:utf-8 -*-

from gevent import monkey

monkey.patch_all()    # 把当前程序中的所有io操作都做上标记
import gevent
import random
import requests
from save import Fund_DB
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

class Funds:
    def __init__(self, code):
        with open('proxy.txt') as lines:
            self.proxies = [line.strip() for line in lines]
        self.useragent = UserAgent(verify_ssl=False)
        self.header = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8', 
            'Accept-Encoding': 'gzip, deflate, sdch',
            'Accept-Language': 'zh-CN,zh;q=0.8,en;q=0.6', 
            'Connection': 'keep-alive'}
        self.code = code
        self.data = []

    def get_url(self, url, params=None):
        self.header['User-Agent'] = self.useragent.random
        proxy = random.choice(self.proxies)
        h,_,__ = proxy.split('/')
        proxies = {h[:-1] : proxy}
        try:
            rsp = requests.get(url, params=params, headers = self.header, proxies=None)
        except:
            print("cant't request {}".format(url))
            pass
        else:
            return rsp.text
        return ''

    def get_fund_init(self):
        # get_fund_data('110021', '2018-02-22', '2019-01-02')
        url = 'http://fund.eastmoney.com/f10/F10DataApi.aspx'
        params = {'type': 'lsjz', 'code': self.code}
        html = self.get_url(url, params)
        content = str(html[13:-2])
        content_split = content.split(',')
        self.pages = int(content_split[-2].split(':')[-1])
        self.records = int(content_split[-3].split(':')[-1])

    def get_fund_data(self, page=1, start='', end=''):
        # get_fund_data('110021', '2018-02-22', '2019-01-02')
        url = 'http://fund.eastmoney.com/f10/F10DataApi.aspx'
        params = {'type': 'lsjz', 'code': self.code, 'page': page, 'per': 20, 'sdate': start, 'edate': end}
        html = self.get_url(url, params)
        
        soup = BeautifulSoup(html, 'lxml')
        tr_datas = soup.select('tbody > tr')
        for tr_data in tr_datas:
            self.data.append(' '.join([td.text for td in tr_data.select('td')]))

    def get_funds(self):
        fund_list = []
        self.get_fund_init()
        threads = [gevent.spawn(self.get_fund_data, page) for page in range(1,self.pages)]
        gevent.joinall(threads)
        for v in self.data:
            try:
                d,v1,v2,r,s1,s2 = v.strip().split(' ')
                v1,v2,r = float(v1),float(v2),float(r[:-1])
            except:
                pass
            else:
                fund_list.append((d,v1,v2,r))
        return fund_list

    def update(self,code):
        

if __name__ == "__main__":
    
    invalid_funds = []
    fdb = Fund_DB('funds.db')

    with open('funds1.txt') as lines:
        for line in lines:
            try:
                code,name = line[1:7],line[8:]
                fund = Funds(code)
                fund_list = fund.get_funds()
                
                fdb.connect_code(code)
                fdb.insert_many(code,fund_list)
            except:
                print(code)
                invalid_funds.append(code)
            else:
                print('{}_{} save ok'.format(code,name.strip()))
    print(invalid_funds)
