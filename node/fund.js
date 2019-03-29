var superagent = require('superagent')
var charset = require('superagent-charset')
var request = charset(superagent)
var cheerio = require('cheerio')
var fs = require('fs')

var getcodes = (callback) => {
    request.get('http://fund.eastmoney.com/allfund.html')
        .charset('gbk')
        .buffer(true)
        .end((err, res) => {
            var $ = cheerio.load(res.text, {
                ignoreWhitespace: true,
                xmlMode: true
            })
            $('.num_right > li > div').each((i, elem) => {
                var codeinfo = $(elem).find('a').eq(0).text()
                code = codeinfo.slice(1, 7)
                info = codeinfo.slice(8)
                callback(code)
            })

        })
}

// getcodes((code) => {
//     fs.appendFileSync('./codes.txt', code, function (err) {
//         if (err) console.log('写文件操作失败');
//         else console.log('写文件操作成功');
//     });
// })
var getfunds = (code, page, callback) => {
    request.get('http://fund.eastmoney.com/f10/F10DataApi.aspx')
        .charset('utf8')
        .buffer(true)
        .query({ 'type': 'lsjz', 'code': code, 'page': page, 'per': '20' })
        .end((err, res) => {
            var $ = cheerio.load(res.text, {
                ignoreWhitespace: true,
                xmlMode: true
            })
            $('tbody > tr').each((i, elem) => {
                var d = $(elem).find('td').eq(0).text()
                var v1 = $(elem).find('td').eq(1).text()
                var v2 = $(elem).find('td').eq(2).text()
                var r = $(elem).find('td').eq(3).text()
                if (v1.trim() !== "" && r.trim() !== "") {
                    callback(code, d + " " + v1 + " " + v2 + " " + r + "\n")
                }
            })
        })
}


var getpages = (code, callback) => {
    request.get('http://fund.eastmoney.com/f10/F10DataApi.aspx')
        .charset('utf8')
        .buffer(true)
        .query({ 'type': 'lsjz', 'code': code, })
        .end((err, res) => {
            var regexp = /pages:(.+?),curpage/
            var pages = Number(regexp.exec(res.text)[1])
            callback(code, pages)
        })
}

var getalldata = (fundcode, callback) => {
    getpages(fundcode, (code, pages) => {
        for (var i = 1; i <= pages; i++) {
            getfunds(code, i.toString(), (code, funds) => {
                callback(funds)
            })
        }
    })
}
var codes = ['000011', '000013', '000014', '000015']


for (var code of codes) {
    console.log(code)
    getalldata(code, (funds) => {
        fs.appendFileSync(code + ".txt", funds, function (err) {
            if (err) console.log('写文件操作失败');
            else console.log('写文件操作成功');
        });
    })
}

