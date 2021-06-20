# coding=utf-8

from gevent import monkey

monkey.patch_all()    # 把当前程序中的所有io操作都做上标记
import re
import requests
from bs4 import BeautifulSoup
import gevent

class Proxy:
    def __init__(self,start,end,url):
        self.start = start
        self.end = end
        self.__ips = []
        self.ips = []
        self.url = 'http://'+url
        self.setup()
    def setup(self):
        self.get_ips()
        threads = [gevent.spawn(self.verify_ip, ip) for ip in self.__ips]
        gevent.joinall(threads)

    def get_ip(self, page):
        url = "http://www.xicidaili.com/nn/%d" % (page)
        headers = {
                "Accept":"text/html,application/xhtml+xml,application/xml;",
                "Accept-Encoding":"gzip, deflate, sdch",
                "Accept-Language":"zh-CN,zh;q=0.8,en;q=0.6",
                "Referer":"http://www.xicidaili.com",
                "User-Agent":"Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.90 Safari/537.36"
                }
        r = requests.get(url,headers=headers)
        soup = BeautifulSoup(r.text, 'lxml')
        data = soup.table.find_all("td")
        ip_compile= re.compile(r'<td>(\d+\.\d+\.\d+\.\d+)</td>')
        port_compile = re.compile(r'<td>(\d+)</td>')
        http_compile = re.compile(r'<td>(HTTPS?)</td>')
        cont = str(data)
        ip = re.findall(ip_compile,cont)
        port = re.findall(port_compile,cont)
        http = re.findall(http_compile,cont)
        return ['{}://{}:{}'.format(h.lower(),i,p) for h,i,p in zip(http,ip,port)]
    
    def get_ips(self):
        for page in range(self.start, self.end):
            self.__ips += self.get_ip(page)

    def verify_ip(self, ip):
        headers = {
                "Accept":"text/html,application/xhtml+xml,application/xml;",
                "Accept-Encoding":"gzip, deflate, sdch",
                "Accept-Language":"zh-CN,zh;q=0.8,en;q=0.6",
                "User-Agent":"Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.90 Safari/537.36"
            }
        try:
            h,_,__=ip.split('/')
            proxies = {h[:-1]:ip}
            requests.get(self.url,headers=headers,proxies=proxies,timeout=2)
        except:
            pass
        else:
            self.ips.append(ip)


if __name__ == '__main__':
    proxy = Proxy(1,3,'fund.eastmoney.com/allfund.html')
    with open('proxy.txt','w') as f:
        for ip in proxy.ips:
            f.write(ip + '\n')


