var str="page:64,curpage";
var r = /page:(.+?),curpage/g;
 
console.log(str.match(r));
console.log(r.exec(str))