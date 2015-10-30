'use strict';
phonecatApp.controller("CourseListCtrl",['$scope','$http','$location', function ($scope, $http, $location){   //18 row
		$scope.title = 'Telephone';

		console.log('$location.url() -', $location.url());
		console.log('$location.path() -', $location.path());
		console.log('$location.search() -', $location.search());
		console.log('$location.hash() -', $location.hash());

		$http.get('phones/phones.json').success(function(data, status, headers, config){
			console.log('this is data:',data,'nn This is status:',status,'nn This is headers:',headers,'this is config:', config);
			$scope.phones = data;
		}).error(function(){});

		var date = new Date();
		$scope.today = date;

		$scope.sortField=undefined;
		$scope.reverse=false;

		$scope.sort = function(fieldName){
			if($scope.sortField === fieldName){
				$scope.reverse = !$scope.reverse;
			} else{
				$scope.sortField = fieldName;
				$scope.reverse = false;
			}
		};
}]);
/* Controllers */
