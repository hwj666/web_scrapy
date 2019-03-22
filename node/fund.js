var superagent = require('superagent')
var charset = require('superagent-charset')
var request = charset(superagent)
var cheerio = require('cheerio')

var getcodes = (callback) => {
    request.get('http://fund.eastmoney.com/allfund.html')
        .charset('gbk')
        .buffer(true)
        .end((err, res) => {
            var codes = []
            var $ = cheerio.load(res.text, {
                ignoreWhitespace: true,
                xmlMode: true
            })
            $('.num_right > li > div').each((i, elem) => {
                var codeinfo = $(elem).find('a').eq(0).text()
                code = codeinfo.slice(1, 7)
                info = codeinfo.slice(8)
                codes.push({ 'code': code, 'info': info })
            })
            callback(codes)
        })
}
// getcodes((codes) => { 
//     for (var code of codes){
//         console.log(code)
//     }
//  })
var getfunds = (code, page, callback) => {
    request.get('http://fund.eastmoney.com/f10/F10DataApi.aspx')
        .charset('utf8')
        .buffer(true)
        .query({ 'type': 'lsjz', 'code': code, 'page': page, 'per': '20' })
        .end((err, res) => {
            var funds = []
            var $ = cheerio.load(res.text, {
                ignoreWhitespace: true,
                xmlMode: true
            })
            $('tbody > tr').each((i, elem) => {
                var d = $(elem).find('td').eq(0).text()
                var v1 = $(elem).find('td').eq(1).text()
                var v2 = $(elem).find('td').eq(2).text()
                var r = $(elem).find('td').eq(3).text()
                funds.push({ 'd': d, 'v1': v1, 'v2': v2, 'r': r })
            })
            callback(code, funds)
        })
}


// getfunds('502023','1',(code,funds)=>{console.log(funds)})

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
// getpages('502023',(code,pages)=>{
//     console.log(code, pages);
// })

var getalldata = (callback) => {
    getpages('502023', (code, pages) => {
        var alldata = []
        for (var i = 1; i <= pages; i++) {
            getfunds(code, i.toString(), (code, funds) => {
                alldata.push(funds)
                console.log(funds)
            })
        }
        callback(alldata)
    })
}

getalldata((data)=>{console.log(data)})

// getcodes((codes) => {
//     for (var codeinfo of codes) {
//         var code = codeinfo['code']
//         getpages(code, (code, pages) => {
//             console.log(code, pages);
//         })
//     }
// })
