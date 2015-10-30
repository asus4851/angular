'use strict';
var phonecatApp = angular.module("phonecatApp",['ngRoute']);
phonecatApp.config(['$routeProvider', function($routeProvide){
		$routeProvide
			.when('',{
			templateUrl:'template/home.html',
			controller:'CourseListCtrl'
			})
			.when('',{
			templateUrl:'template/about.html',
			controller:'AboutCtrl'
			})
			.when('',{
			templateUrl:'template/contact.html',
			controller:'ContactCtrl'
			})
			.otherwise({
				redirectTo:'/'
			});
}]);
/* App Module */
